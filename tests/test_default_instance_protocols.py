"""Reject instance reconstruction protocols before copying their hidden data."""

import copy
import dataclasses
from typing import Any

import pytest
from pydantic import BaseModel

from msgspec_fastapi.defaults import copy_body_default, copy_metadata


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_instance_getstate_does_not_copy_model_arguments(operation: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    @dataclasses.dataclass
    class Record:
        value: Any

    hidden = External(value=7)
    original = Record(1)
    original.__getstate__ = lambda: {'value': hidden}
    assert copy.deepcopy(original).value.value == 7
    assert calls == ['model-copy']
    calls.clear()
    with pytest.raises(TypeError, match='Instance state methods'):
        operation(original)
    assert calls == []


@pytest.mark.parametrize(
    'name',
    [
        '__reduce__',
        '__reduce_ex__',
        '__getnewargs__',
        '__getnewargs_ex__',
        '__setstate__',
        '__getattribute__',
        '__getattr__',
    ],
)
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_instance_state_protocols_are_rejected_before_application_callbacks(
    name: str, operation: Any
) -> None:
    calls = []

    @dataclasses.dataclass
    class Record:
        value: int

    def protocol(*args: Any) -> Any:
        calls.append(name)
        raise RuntimeError('Instance state callback executed')

    original = Record(1)
    setattr(original, name, protocol)
    with pytest.raises(TypeError, match='Instance state methods'):
        operation(original)
    assert calls == []


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_plain_instance_copy_function_still_runs_once(operation: Any) -> None:
    calls = []

    @dataclasses.dataclass
    class Record:
        value: int

    original = Record(1)

    def duplicate(memo: Any) -> Record:
        calls.append('application-copy')
        return Record(9)

    original.__deepcopy__ = duplicate
    result = operation(original)
    assert result.value == 9
    assert calls == ['application-copy']


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_instance_copy_result_still_requires_supported_data(operation: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    @dataclasses.dataclass
    class Record:
        value: int

    hidden = External(value=7)
    original = Record(1)

    def duplicate(memo: Any) -> Any:
        calls.append('application-copy')
        return hidden

    original.__deepcopy__ = duplicate
    with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
        operation(original)
    assert calls == ['application-copy']


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
@pytest.mark.parametrize('position', ['class', 'instance'])
def test_explicit_copy_function_precedes_registered_reducer(
    operation: Any, position: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import copyreg

    calls = []

    @dataclasses.dataclass
    class Record:
        value: int

    def duplicate(self: Any, memo: Any) -> Record:
        calls.append('application-copy')
        return Record(self.value + 10)

    original = Record(1)
    if position == 'class':
        Record.__deepcopy__ = duplicate
    else:

        def with_copy(value: int) -> Record:
            instance = Record(value)

            def duplicate_instance(memo: Any) -> Record:
                calls.append('application-copy')
                return with_copy(instance.value + 10)

            instance.__deepcopy__ = duplicate_instance
            return instance

        original = with_copy(1)

    def reducer(value: Any) -> Any:
        calls.append('reducer')
        raise AssertionError('Ignored registered reducer executed')

    monkeypatch.setitem(copyreg.dispatch_table, Record, reducer)
    assert copy.deepcopy(original).value == 11
    assert calls == ['application-copy']
    calls.clear()
    assert operation(original).value == 11
    assert calls == ['application-copy']


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_instance_copy_result_cannot_expose_reducer_on_framework_recopy(
    operation: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import copyreg

    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    @dataclasses.dataclass
    class Record:
        value: Any

    hidden = External(value=7)
    original = Record(1)

    def duplicate(memo: Any) -> Record:
        calls.append('application-copy')
        return Record(11)

    original.__deepcopy__ = duplicate

    def reducer(value: Any) -> Any:
        calls.append('reducer')
        return Record, (hidden,)

    monkeypatch.setitem(copyreg.dispatch_table, Record, reducer)
    first = copy.deepcopy(original)
    assert calls == ['application-copy']
    assert copy.deepcopy(first).value.value == 7
    assert calls == ['application-copy', 'reducer', 'model-copy']
    calls.clear()
    with pytest.raises(TypeError, match='Registered copy reducers'):
        operation(original)
    assert calls == ['application-copy']
