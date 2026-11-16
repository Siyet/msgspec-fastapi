"""Enum identity copying must not invoke hidden model copy protocols."""

import copy
from enum import Enum, IntEnum, StrEnum
from typing import Any

import pytest
from pydantic import BaseModel

from msgspec_fastapi.defaults import copy_body_default, copy_metadata


def choice(enum_class: Any) -> Any:
    value = 'one' if enum_class is StrEnum else 1
    return enum_class('Choice', {'one': value}).one


@pytest.mark.parametrize('enum_class', [Enum, IntEnum, StrEnum])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_bound_model_copy_is_rejected_before_enum_copy(enum_class: Any, operation: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    hidden = External(value=7)
    value = choice(enum_class)
    value.__deepcopy__ = hidden.__deepcopy__
    assert copy.deepcopy(value).value == 7
    assert calls == ['model-copy']
    calls.clear()
    with pytest.raises(TypeError, match='Instance copy methods'):
        operation(value)
    assert calls == []


@pytest.mark.parametrize('kind', ['builtin', 'opaque', 'none'])
@pytest.mark.parametrize('enum_class', [Enum, IntEnum, StrEnum])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_nonfunction_instance_copier_is_rejected(
    kind: str, enum_class: Any, operation: Any
) -> None:
    calls = []

    class Opaque:
        def __call__(self, memo: Any) -> Any:
            calls.append('opaque-copy')
            raise RuntimeError('Unsupported copy callback executed')

    value = choice(enum_class)
    value.__deepcopy__ = {'builtin': calls.append, 'opaque': Opaque(), 'none': None}[kind]
    with pytest.raises(TypeError, match='Instance copy methods'):
        operation(value)
    assert calls == []


@pytest.mark.parametrize('name', ['__getstate__', '__reduce_ex__'])
@pytest.mark.parametrize('enum_class', [Enum, IntEnum, StrEnum])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_enum_instance_state_protocols_are_rejected(
    name: str, enum_class: Any, operation: Any
) -> None:
    calls = []

    def state(*args: Any) -> Any:
        calls.append(name)
        raise RuntimeError('Unsupported state callback executed')

    value = choice(enum_class)
    setattr(value, name, state)
    with pytest.raises(TypeError, match='Instance state methods'):
        operation(value)
    assert calls == []


@pytest.mark.parametrize('enum_class', [Enum, IntEnum, StrEnum])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_native_and_plain_function_enum_copy_remain_supported(
    enum_class: Any, operation: Any
) -> None:
    calls = []
    value = choice(enum_class)
    assert operation(value) is value

    def duplicate(memo: Any) -> Any:
        calls.append('application-copy')
        return value

    value.__deepcopy__ = duplicate
    assert operation(value) is value
    assert calls == ['application-copy']


@pytest.mark.parametrize('enum_class', [Enum, IntEnum, StrEnum])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_plain_enum_copy_result_is_still_checked(enum_class: Any, operation: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    hidden = External(value=7)
    value = choice(enum_class)

    def duplicate(memo: Any) -> Any:
        calls.append('application-copy')
        return hidden

    value.__deepcopy__ = duplicate
    with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
        operation(value)
    assert calls == ['application-copy']


@pytest.mark.parametrize('enum_class', [Enum, IntEnum, StrEnum])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_enum_identity_copy_does_not_copy_unrelated_member_state(
    enum_class: Any, operation: Any
) -> None:
    calls = []

    class Uncopied:
        def __deepcopy__(self, memo: Any) -> Any:
            calls.append('unrelated-copy')
            raise RuntimeError('Unrelated Enum state copied')

    value = choice(enum_class)
    value.extra = Uncopied()
    assert operation(value) is value
    assert calls == []


def test_none_override_cannot_copy_model_enum_value_during_metadata_copy() -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    hidden = External(value=7)
    value = Enum('Choice', {'one': hidden}).one
    value.__deepcopy__ = None
    assert copy.deepcopy(value) is value
    assert calls == ['model-copy']
    calls.clear()
    with pytest.raises(TypeError, match='Instance copy methods'):
        copy_metadata(value)
    assert calls == []
