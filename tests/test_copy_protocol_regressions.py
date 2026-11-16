"""Copied defaults retain stdlib memo, callback and type-snapshot behavior."""

import copy
import copyreg
import dataclasses
import sys
import weakref
from collections.abc import Callable
from typing import Any

import msgspec
import pytest

from msgspec_fastapi import defaults
from msgspec_fastapi._defaults import _Copier


@pytest.fixture
def native_copy() -> Callable[..., Any]:
    copier = _Copier(
        copy_module=copy,
        deepcopy=copy.deepcopy,
        list_copier=copy._deepcopy_list,
        dict_copier=copy._deepcopy_dict,
        tuple_copier=copy._deepcopy_tuple,
        atomic_copier=getattr(copy, '_deepcopy_atomic', None),
        reconstruct=copy._reconstruct,
        atomic_first=sys.version_info >= (3, 14),
        memo_sentinel=copy.deepcopy.__defaults__[-1],
    )
    return copier.deepcopy


def run_case(
    operation: str, copier: Callable[..., Any], case: Callable[[Callable[..., Any]], Any]
) -> Any:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(defaults, 'deepcopy', copier)
        operations = {
            'direct': copier,
            'body': defaults.copy_body_default,
            'metadata': defaults.copy_metadata,
        }
        return case(operations[operation])


@pytest.mark.parametrize('operation', ['direct', 'body', 'metadata'])
@pytest.mark.parametrize('kind', ['dataclass', 'struct', 'list', 'dict'])
def test_initial_memo_sentinel_matches_stdlib(
    native_copy: Callable[..., Any], operation: str, kind: str
) -> None:
    @dataclasses.dataclass
    class Record:
        value: int

    class Struct(msgspec.Struct):
        value: int

    def run(copier: Callable[..., Any]) -> Any:
        target = (
            Record(7)
            if kind == 'dataclass'
            else Struct(7)
            if kind == 'struct'
            else [7]
            if kind == 'list'
            else {'value': 7}
        )

        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: dict[int, Any]) -> Any:
                memo[id(target)] = copy.deepcopy.__defaults__[-1]
                return self

        return copier([Setter(0), target])[1]

    expected = run_case(operation, copy.deepcopy, run)
    actual = run_case(operation, native_copy, run)
    assert type(actual) is type(expected) and actual == expected


@pytest.mark.parametrize('operation', ['direct', 'body', 'metadata'])
def test_tuple_second_memo_lookup_preserves_sentinel(
    native_copy: Callable[..., Any], operation: str
) -> None:
    sentinel = copy.deepcopy.__defaults__[-1]

    def run(copier: Callable[..., Any]) -> Any:
        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: dict[int, Any]) -> Any:
                memo[id(original)] = sentinel
                return self

        original = (Setter(0),)
        return copier(original)

    assert run_case(operation, copy.deepcopy, run) is sentinel
    assert run_case(operation, native_copy, run) is sentinel


@pytest.mark.skipif(sys.version_info < (3, 14), reason='Atomic-first copying starts in Python 3.14')
@pytest.mark.parametrize('operation', ['direct', 'metadata'])
@pytest.mark.parametrize('kind', ['function', 'class', 'builtin'])
def test_atomic_references_precede_memo_on_python314(
    native_copy: Callable[..., Any], operation: str, kind: str
) -> None:
    @dataclasses.dataclass
    class Record:
        value: int

    def function() -> None:
        pass

    target = function if kind == 'function' else Record if kind == 'class' else len

    def run(copier: Callable[..., Any]) -> Any:
        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: dict[int, Any]) -> Any:
                memo[id(target)] = 'overridden'
                return self

        return copier([Setter(0), target])[1]

    assert run_case(operation, copy.deepcopy, run) is target
    assert run_case(operation, native_copy, run) is target


@pytest.mark.skipif(sys.version_info < (3, 14), reason='Python 3.14 has a separate atomic type set')
@pytest.mark.parametrize('operation', ['direct', 'body', 'metadata'])
def test_removed_atomic_type_uses_current_reducer(
    native_copy: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    atomics = set(copy._atomic_types)
    atomics.remove(int)
    monkeypatch.setattr(copy, '_atomic_types', atomics)
    monkeypatch.setitem(copyreg.dispatch_table, int, lambda value: (str, ('changed',)))

    def run(copier: Callable[..., Any]) -> Any:
        return copier([42])

    assert run_case(operation, copy.deepcopy, run) == ['changed']
    assert run_case(operation, native_copy, run) == ['changed']


@pytest.mark.parametrize('operation', ['direct', 'body', 'metadata'])
def test_false_reducer_release_follows_next_lookup(
    native_copy: Callable[..., Any], operation: str
) -> None:
    def run(copier: Callable[..., Any]) -> Any:
        active = False
        calls = []

        class Value(msgspec.Struct):
            value: int

            def __post_init__(self) -> None:
                if active:
                    calls.append(('post', self.value))
                    if self.value == 1:
                        copyreg.dispatch_table[Value] = Reducer()
                    else:
                        Value.__reduce_ex__ = object.__reduce_ex__

        def replaced(self: Any, protocol: int) -> tuple[Any, tuple[int]]:
            calls.append(('replaced', self.value))
            return Value, (self.value + 10,)

        class Reducer:
            def __bool__(self) -> bool:
                calls.append(('bool',))
                copyreg.dispatch_table.pop(Value, None)
                return False

            def __del__(self) -> None:
                calls.append(('del',))
                Value.__reduce_ex__ = replaced

        original = [Value(1), Value(2)]
        active = True
        try:
            result = copier(original)
            return [item.value for item in result], calls
        finally:
            copyreg.dispatch_table.pop(Value, None)

    expected = run_case(operation, copy.deepcopy, run)
    assert expected[0] == [1, 2]
    assert run_case(operation, native_copy, run) == expected


@pytest.mark.parametrize('operation', ['direct', 'body', 'metadata'])
def test_copy_hook_release_follows_memoization(
    native_copy: Callable[..., Any], operation: str
) -> None:
    def run(copier: Callable[..., Any]) -> Any:
        state: dict[str, Any] = {}
        calls = []

        @dataclasses.dataclass
        class Target:
            value: int

        class Value(msgspec.Struct):
            value: int

        target = Target(7)
        original = Value(1)

        def copied(self: Any, memo: dict[int, Any]) -> Any:
            state['memo'] = memo
            del Value.__deepcopy__
            return Value(self.value + 10)

        def released(reference: weakref.ReferenceType[Any]) -> None:
            memo = state['memo']
            stage = 'memoized' if id(original) in memo else 'not memoized'
            calls.append(stage)
            memo[id(target)] = stage

        reference = weakref.ref(copied, released)
        Value.__deepcopy__ = copied
        del copied
        result = copier([original, target])
        assert reference() is None
        return [result[0].value, result[1]], calls

    expected = run_case(operation, copy.deepcopy, run)
    assert expected == ([11, 'memoized'], ['memoized'])
    assert run_case(operation, native_copy, run) == expected


@pytest.mark.parametrize('operation', ['direct', 'body', 'metadata'])
def test_dispatch_lookup_retains_original_class_snapshot(
    native_copy: Callable[..., Any], operation: str
) -> None:
    def run(copier: Callable[..., Any]) -> Any:
        active = False
        calls = []

        class Meta(type):
            def __hash__(cls) -> int:
                nonlocal active
                if active and cls is Before:
                    active = False
                    target.__class__ = After
                    calls.append('class changed')
                return type.__hash__(cls)

        @dataclasses.dataclass
        class Before(metaclass=Meta):
            value: int

        @dataclasses.dataclass
        class After(metaclass=Meta):
            value: int

        def make(value: int) -> Any:
            copyreg.dispatch_table.pop(Before, None)
            copyreg.dispatch_table.pop(After, None)
            return Before(value)

        def reduce_before(value: Any) -> tuple[Any, tuple[int]]:
            calls.append('before reducer')
            return make, (value.value + 10,)

        def reduce_after(value: Any) -> tuple[Any, tuple[int]]:
            calls.append('after reducer')
            return make, (value.value + 100,)

        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: dict[int, Any]) -> Any:
                nonlocal active
                copyreg.dispatch_table[Before] = reduce_before
                copyreg.dispatch_table[After] = reduce_after
                active = True
                return self

        target = Before(1)
        try:
            result = copier([Setter(0), target])
            return result[1].value, calls
        finally:
            copyreg.dispatch_table.pop(Before, None)
            copyreg.dispatch_table.pop(After, None)

    expected = run_case(operation, copy.deepcopy, run)
    assert expected == (11, ['class changed', 'before reducer'])
    assert run_case(operation, native_copy, run) == expected
