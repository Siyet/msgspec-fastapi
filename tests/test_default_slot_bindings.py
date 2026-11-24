"""Declared and cached slot names must not expose property-backed copy state."""

import copy
import dataclasses
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

import pytest
from pydantic import BaseModel

from msgspec_fastapi.defaults import _check_copy_graph, copy_body_default, copy_metadata


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
def test_same_declaring_class_slot_replacement_is_rejected_before_getter(operation: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    hidden = External(value=7)

    @dataclasses.dataclass
    class Record:
        value: int

    class Slotted(Record):
        __slots__ = ('extra',)

    original = Slotted(1)
    original.extra = None

    def replaced(self: Any) -> Any:
        calls.append('slot-getter')
        return hidden

    Slotted.extra = property(replaced)
    with pytest.raises(AttributeError):
        copy.deepcopy(original)
    assert calls == ['slot-getter', 'model-copy']
    calls.clear()
    if '__slotnames__' in Slotted.__dict__:
        del Slotted.__slotnames__
    with pytest.raises(TypeError, match='Non-native slot bindings'):
        operation(original)
    assert calls == []


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
def test_forged_cached_slot_name_is_rejected_before_getter(operation: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    hidden = External(value=7)

    @dataclasses.dataclass
    class Record:
        value: int

        @property
        def exposed(self) -> Any:
            calls.append('slot-getter')
            return hidden

    Record.__slotnames__ = ['exposed']
    original = Record(1)
    with pytest.raises(AttributeError):
        copy.deepcopy(original)
    assert calls == ['slot-getter', 'model-copy']
    calls.clear()
    with pytest.raises(TypeError, match='Non-native slot bindings'):
        operation(original)
    assert calls == []


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
@pytest.mark.parametrize('kind', ['declared', 'cached'])
def test_slot_name_iterable_is_rejected_without_application_iteration(
    operation: Any, kind: str
) -> None:
    calls = []

    @dataclasses.dataclass
    class Record:
        value: int

    class Slotted(Record):
        __slots__ = ('extra',)

    class Names:
        def __iter__(self) -> Any:
            calls.append('slot-iteration')
            raise RuntimeError('Application slot iterable executed')

    original = Slotted(1)
    original.extra = None
    if kind == 'declared':
        Slotted.__slots__ = Names()
    else:
        Slotted.__slotnames__ = Names()
    with pytest.raises(TypeError, match='slot'):
        operation(original)
    assert calls == []


@pytest.mark.parametrize('container', [str, tuple, list, set, frozenset, dict])
@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_closed_slot_declarations_keep_native_slot_copy(container: Any, operation: Any) -> None:
    declaration = (
        'extra'
        if container is str
        else {'extra': 'native slot'}
        if container is dict
        else container(['extra'])
    )

    @dataclasses.dataclass
    class Record:
        value: int

    class Slotted(Record):
        __slots__ = declaration

    original = Slotted(1)
    original.extra = [7]
    result = operation(original)
    assert result.value == 1 and result.extra == [7]
    assert result.extra is not original.extra


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_private_inherited_and_special_native_slots_remain_supported(operation: Any) -> None:
    @dataclasses.dataclass
    class __Base:
        __slots__ = ('__value', '__dict__', '__weakref__')
        __value: int

    class _Child(__Base):
        __slots__ = ('__extra',)

    original = _Child(7)
    original._Child__extra = [9]
    result = operation(original)
    assert result._Base__value == 7 and result._Child__extra == [9]
    assert result._Child__extra is not original._Child__extra


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_frozen_slotted_dataclass_native_state_remains_supported(operation: Any) -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class Record:
        value: list[int]

    original = Record([7])
    result = operation(original)
    assert result.value == [7] and result.value is not original.value


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
def test_private_slot_validation_uses_native_class_name(operation: Any) -> None:
    calls = []
    active = False

    class ReadonlyMeta(type):
        def __getattribute__(cls, name: str) -> Any:
            if active and name == '__name__':
                calls.append('metaclass-name')
                raise RuntimeError('Application class-name lookup executed')
            return super().__getattribute__(name)

        def __setattr__(cls, name: str, value: Any) -> None:
            if name == '__slotnames__':
                raise RuntimeError('Readonly slot-name cache')
            super().__setattr__(name, value)

    @dataclasses.dataclass
    class _Record(metaclass=ReadonlyMeta):
        __slots__ = ('__value',)
        __value: int

    original = _Record(7)
    type.__setattr__(_Record, '__slotnames__', ['_Record__value'])
    active = True
    result = operation(original)
    if operation is not _check_copy_graph:
        assert result._Record__value == 7
    assert calls == []


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_private_slot_in_all_underscore_class_name_remains_supported(operation: Any) -> None:
    @dataclasses.dataclass
    class ___:
        __slots__ = ('__value',)
        __value: int

    result = operation(___(7))
    assert result.__value == 7


@pytest.mark.parametrize('shape', [tuple, set, frozenset, dict])
@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
def test_nonlist_cached_slot_names_are_rejected(shape: Any, operation: Any) -> None:
    @dataclasses.dataclass
    class Record:
        __slots__ = ('value',)
        value: int

    Record.__slotnames__ = {'value': 7} if shape is dict else shape(['value'])
    with pytest.raises(TypeError, match='__slotnames__ should be a list or None'):
        copy.deepcopy(Record(7))
    with pytest.raises(TypeError, match='cached slot names'):
        operation(Record(7))


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
def test_enum_identity_copy_ignores_unrelated_slot_metadata(operation: Any) -> None:
    from enum import Enum

    calls = []

    class Names:
        def __iter__(self) -> Any:
            calls.append('slot-iteration')
            raise RuntimeError('Unrelated Enum slots inspected')

    class Choice(Enum):
        one = 1

    Choice.__slots__ = Names()
    Choice.__slotnames__ = Names()
    assert operation(Choice.one) is Choice.one
    assert calls == []


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
@pytest.mark.parametrize('kind', ['declared', 'cached'])
def test_slot_name_subclass_is_rejected_before_string_callbacks(operation: Any, kind: str) -> None:
    calls = []

    class Name(str):
        def startswith(self, *args: Any) -> Any:
            calls.append('string-prefix')
            raise RuntimeError('Application string callback executed')

        def __hash__(self) -> int:
            calls.append('string-hash')
            raise RuntimeError('Application string callback executed')

    @dataclasses.dataclass
    class Record:
        value: int

    class Slotted(Record):
        __slots__ = ('extra',)

    original = Slotted(1)
    original.extra = None
    if kind == 'declared':
        Slotted.__slots__ = [Name('extra')]
    else:
        Slotted.__slotnames__ = [Name('extra')]
    with pytest.raises(TypeError, match='exact strings'):
        operation(original)
    assert calls == []


@pytest.mark.parametrize('operation', [copy_body_default, copy_metadata])
@pytest.mark.parametrize('path', [Path('a/b'), PureWindowsPath('c:/a')])
def test_standard_library_paths_are_copied(operation: Any, path: PurePath) -> None:
    result = operation({'path': path})['path']
    assert result == path and type(result) is type(path)


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
def test_standard_library_path_slot_values_are_checked(operation: Any) -> None:
    path = PurePath('a')
    PurePath.__dict__['_str'].__set__(path, object())
    with pytest.raises(TypeError, match='Unsupported object'):
        operation({'path': path})


@pytest.mark.parametrize('operation', [_check_copy_graph, copy_body_default, copy_metadata])
def test_undeclared_slot_shadowed_by_subclass_property_is_rejected(operation: Any) -> None:
    calls = []

    class Other:
        __slots__ = ('extra',)

    @dataclasses.dataclass
    class Record:
        value: int

    Record.extra = Other.__dict__['extra']

    class Child(Record):
        @property
        def extra(self) -> Any:
            calls.append('getter')
            return None

    with pytest.raises(TypeError, match='Shadowed slots'):
        operation(Child(1))
    assert calls == []
