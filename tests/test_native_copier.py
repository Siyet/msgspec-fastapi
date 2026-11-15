import copy
import copyreg
import dataclasses
import gc
import inspect
import sys
import types
import weakref
from typing import Any

import msgspec
import pytest

from msgspec_fastapi._defaults import _Copier
from msgspec_fastapi.defaults import deepcopy as accelerated


def make_copier(**overrides: Any) -> Any:
    values = dict(
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
    values.update(overrides)
    return _Copier(**values)


def test_named_signatures_and_gc_tracking() -> None:
    assert len(inspect.signature(_Copier).parameters) == 9
    assert str(inspect.signature(make_copier().deepcopy)) == '(value, memo=None, /)'
    assert gc.is_tracked(make_copier())


def test_uninitialized_instance_and_retry() -> None:
    empty = _Copier.__new__(_Copier)
    with pytest.raises(TypeError, match='not initialized'):
        empty(1)
    with pytest.raises(TypeError, match='not initialized'):
        empty.deepcopy(1)
    with pytest.raises(TypeError):
        empty.__init__()
    with pytest.raises(TypeError):
        make_copier().__init__()
    assert make_copier()(42) == 42


@pytest.mark.parametrize(
    'field,value',
    [
        ('copy_module', {}),
        ('deepcopy', None),
        ('list_copier', None),
        ('dict_copier', None),
        ('tuple_copier', None),
        ('atomic_copier', 0),
        ('reconstruct', None),
        ('atomic_first', 1),
    ],
)
def test_malformed_configuration_is_rejected(field: Any, value: Any) -> None:
    with pytest.raises(TypeError):
        make_copier(**{field: value})


def test_fallback_globals_cycle_is_collected() -> None:

    def template(value: Any, memo: Any) -> Any:
        return value

    namespace = {}
    callback = types.FunctionType(template.__code__, namespace)
    reference = weakref.ref(callback)
    owner = make_copier(deepcopy=callback)
    namespace['owner'] = owner
    del namespace, callback, owner
    gc.collect()
    assert reference() is None


def test_owned_callable_policy_is_not_borrowed() -> None:

    class Function:
        def __call__(self, *args: Any) -> Any:
            return copy.deepcopy(*args)

    callback = Function()
    reference = weakref.ref(callback)
    owner = make_copier(deepcopy=callback)
    del callback
    gc.collect()
    assert reference() is not None
    del owner
    gc.collect()
    assert reference() is None


def test_owned_memo_sentinel_is_not_borrowed() -> None:

    class Marker:
        pass

    marker = Marker()
    reference = weakref.ref(marker)
    owner = make_copier(memo_sentinel=marker)
    del marker
    gc.collect()
    assert reference() is not None
    del owner
    gc.collect()
    assert reference() is None


def test_nonexact_memo_delegates_before_observable_lookups() -> None:

    def run(operation: Any) -> Any:
        events = []

        class Memo(dict):
            def get(self, key: Any, default: Any = None) -> Any:
                events.append('get')
                return super().get(key, default)

        return (operation([1], Memo()), events)

    assert run(make_copier()) == run(copy.deepcopy)


@pytest.mark.parametrize('value', [None, False, 42, 1.5, 'text', b'text'])
def test_existing_atomic_memo(value: Any) -> None:
    assert accelerated(value, {id(value): 'memo'}) == copy.deepcopy(value, {id(value): 'memo'})


def test_aliases_mutable_and_tuple_cycles() -> None:
    leaf = []
    pair = (leaf,)
    leaf.extend([pair, leaf])
    original = [pair, leaf, pair]
    out = accelerated(original)
    assert out[0] is out[2]
    assert out[0][0] is out[1]
    assert out[1][0] is out[0] and out[1][1] is out[1]


def test_tuple_second_memo_read() -> None:

    class Child:
        def __deepcopy__(self, memo: Any) -> Any:
            memo[id(root)] = 'memo tuple'
            return self

    root = (Child(),)
    assert accelerated(root) == copy.deepcopy(root) == 'memo tuple'


def run_dict_order(fn: Any, mutate: Any = False) -> Any:
    calls = []

    class Key:
        def __hash__(self) -> Any:
            return 0

        def __deepcopy__(self, memo: Any) -> Any:
            calls.append('key')
            return self

    class Value:
        def __deepcopy__(self, memo: Any) -> Any:
            calls.append('value')
            if mutate:
                original['new'] = 1
            return self

    original = {Key(): Value()}
    try:
        fn(original)
    except Exception as exc:
        return (type(exc), str(exc), calls)
    return calls


def test_dict_value_before_key() -> None:
    assert run_dict_order(accelerated) == run_dict_order(copy.deepcopy) == ['value', 'key']


def test_live_dict_iterator_mutation() -> None:
    assert run_dict_order(accelerated, True) == run_dict_order(copy.deepcopy, True)


def test_live_list_iterator_mutation() -> None:

    def run(fn: Any) -> Any:

        class Value:
            def __deepcopy__(self, memo: Any) -> Any:
                original.append('appended')
                return 'copied'

        original = [Value()]
        return fn(original)

    assert run(accelerated) == run(copy.deepcopy) == ['copied', 'appended']


def test_keepalive_bucket_callback() -> None:

    def run(fn: Any) -> Any:
        calls = []

        class Bucket:
            def append(self, value: Any) -> Any:
                calls.append(type(value).__name__)

        class Value:
            def __deepcopy__(self, memo: Any) -> Any:
                memo[id(memo)] = Bucket()
                return 'copied'

        result = fn([Value(), [1]])
        return (result, calls)

    assert run(accelerated) == run(copy.deepcopy)


def test_keepalive_append_keyerror_matches_stdlib() -> None:

    def run(fn: Any) -> Any:
        calls = []

        class Bucket:
            def append(self, value: Any) -> Any:
                calls.append(type(value).__name__)
                raise KeyError('append')

        class Value(msgspec.Struct):
            x: int

            def __deepcopy__(self, memo: Any) -> Any:
                memo[id(memo)] = Bucket()
                return Value(7)

        memo = {}
        result = fn(Value(1), memo)
        return (result.x, calls, [type(x).__name__ for x in memo[id(memo)]])

    assert run(accelerated) == run(copy.deepcopy)


def test_late_struct_copy_hook() -> None:

    def run(fn: Any) -> Any:
        calls = []
        active = False

        class Value(msgspec.Struct):
            x: int

            def __post_init__(self) -> Any:
                if active and self.x == 1:

                    def copied(self: Any, memo: Any) -> Any:
                        calls.append('copy')
                        return Value(self.x + 10)

                    Value.__deepcopy__ = copied

        original = [Value(1), Value(2)]
        del Value.__post_init__
        active = True
        return ([x.x for x in fn(original)], calls)

    assert run(accelerated) == run(copy.deepcopy) == ([1, 12], ['copy'])


def test_kwonly_constructor_and_reduce_snapshot() -> None:

    def run(fn: Any) -> Any:
        calls = []

        class Child:
            def __deepcopy__(self, memo: Any) -> Any:
                original.b = 99
                return 'child'

        class Value(msgspec.Struct, kw_only=True):
            a: object
            b: int

            def __post_init__(self) -> Any:
                calls.append((self.a, self.b))

        original = Value(a=Child(), b=1)
        calls.clear()
        out = fn(original)
        return (out.a, out.b, calls)

    assert run(accelerated) == run(copy.deepcopy) == ('child', 1, [('child', 1)])


def test_dispatch_mutation_for_later_list() -> None:

    def run(fn: Any) -> Any:
        old = copy._deepcopy_dispatch[list]
        calls = []

        class Value(msgspec.Struct):
            x: int

            def __deepcopy__(self, memo: Any) -> Any:

                def changed(x: Any, memo: Any) -> Any:
                    calls.append('list')
                    return ['changed']

                copy._deepcopy_dispatch[list] = changed
                return self

        try:
            result = fn([Value(1), [2]])
            return (result[1], calls)
        finally:
            copy._deepcopy_dispatch[list] = old

    assert run(accelerated) == run(copy.deepcopy) == (['changed'], ['list'])


def test_removed_list_dispatch_fallback_memo_count() -> None:
    old = copy._deepcopy_dispatch.pop(list)
    try:

        def run(fn: Any) -> Any:
            value = [[1]]
            memo = {}
            result = fn(value, memo)
            return (result, [type(x).__name__ for x in memo[id(memo)]])

        assert run(accelerated) == run(copy.deepcopy)
    finally:
        copy._deepcopy_dispatch[list] = old


def test_struct_late_reduce_and_registration() -> None:

    def run(fn: Any) -> Any:
        active = False
        calls = []

        class Value(msgspec.Struct):
            x: int

            def __post_init__(self) -> Any:
                if active and self.x == 1:

                    def reduced(x: Any) -> Any:
                        calls.append('reduce')
                        return (Value, (x.x + 10,))

                    copyreg.dispatch_table[Value] = reduced

        original = [Value(1), Value(2)]
        active = True
        try:
            return ([x.x for x in fn(original)], calls)
        finally:
            copyreg.dispatch_table.pop(Value, None)

    assert run(accelerated) == run(copy.deepcopy) == ([1, 12], ['reduce'])


def test_struct_nonstandard_reduce_state_delegation() -> None:

    def run(fn: Any) -> Any:
        calls = []

        class Value(msgspec.Struct, dict=True):
            x: int

            def __reduce_ex__(self, protocol: Any) -> Any:
                calls.append(protocol)
                return (Value, (self.x,), {'extra': [1]})

        result = fn(Value(2))
        return (result.x, result.extra, calls)

    assert run(accelerated) == run(copy.deepcopy) == (2, [1], [4])


def test_memo_identity_from_user_hook() -> None:

    class Value(msgspec.Struct):
        x: int

        def __deepcopy__(self, memo: Any) -> Any:
            assert memo is expected
            return self

    expected = {}
    accelerated([Value(1)], expected)


def test_struct_return_original_reconstruction_still_memoizes() -> None:

    class Value(msgspec.Struct):
        x: int

        def __reduce_ex__(self, protocol: Any) -> Any:
            return (lambda: original, ())

    original = Value(1)
    for fn in [copy.deepcopy, accelerated]:
        memo = {}
        assert fn(original, memo) is original
        assert memo[id(original)] is original


def test_reentrant_copy_preserves_outer_memo() -> None:
    outer = {}

    class Value(msgspec.Struct):
        x: int

        def __deepcopy__(self, memo: Any) -> Any:
            assert memo is outer
            assert accelerated([42]) == [42]
            assert memo is outer
            return Value(self.x + 1)

    assert accelerated([Value(1), 42], outer)[0].x == 2


def test_escaped_child_callback_is_safe_and_does_not_retain_root() -> None:
    captured = []

    @dataclasses.dataclass
    class Value:
        x: int

        def __reduce_ex__(self, protocol: int) -> Any:
            return object.__reduce_ex__(self, protocol)[:4]

        def __setstate__(self, state: Any) -> Any:
            captured.append(inspect.currentframe().f_back.f_locals['deepcopy'])
            self.__dict__.update(state)

    original = Value(7)
    reference = weakref.ref(original)
    result = accelerated(original)
    assert result == original and len(captured) == 1
    assert captured[0]([1, 2], {}) == [1, 2]
    del original, result
    gc.collect()
    assert reference() is None


def test_reconstruct_replacement_is_positional_without_reduce_replay() -> None:

    def run(fn: Any) -> Any:
        original = copy._reconstruct
        calls = []

        class Value(msgspec.Struct):
            x: int

            def __reduce_ex__(self, protocol: Any) -> Any:
                calls.append(('reduce', protocol))
                return (Value, (self.x,))

        def replacement(x: Any, memo: Any, *rv: Any) -> Any:
            calls.append(('helper', len(rv)))
            return original(x, memo, *rv)

        copy._reconstruct = replacement
        try:
            return (fn(Value(7)).x, calls)
        finally:
            copy._reconstruct = original

    assert run(accelerated) == run(copy.deepcopy) == (7, [('reduce', 4), ('helper', 2)])


def test_constructor_can_replace_helper_for_later_item() -> None:

    def run(fn: Any) -> Any:
        original = copy._reconstruct
        calls = []
        active = False

        def replacement(x: Any, memo: Any, *rv: Any) -> Any:
            calls.append(x.x)
            return original(x, memo, *rv)

        class Value(msgspec.Struct):
            x: int

            def __post_init__(self) -> Any:
                if active and self.x == 1:
                    copy._reconstruct = replacement

        values = [Value(1), Value(2)]
        active = True
        try:
            return ([x.x for x in fn(values)], calls)
        finally:
            copy._reconstruct = original

    assert run(accelerated) == run(copy.deepcopy) == ([1, 2], [2])


def test_missing_reducers_copy_error_type() -> None:

    class Value(msgspec.Struct):
        x: int
        __reduce_ex__ = None
        __reduce__ = None

    for fn in (accelerated, copy.deepcopy):
        with pytest.raises(copy.Error):
            fn(Value(1))


def test_initial_id_key_is_reused_for_outer_memo_assignment() -> None:

    def run(operation: Any) -> Any:
        comparisons = []

        @dataclasses.dataclass
        class Value:
            x: int

        original = Value(7)

        class Key:
            def __hash__(self) -> Any:
                return id(original)

            def __eq__(self, other: Any) -> Any:
                comparisons.append(other)
                return False

        result = operation(original, {Key(): None})
        assert len(comparisons) >= 3
        return (result.x, comparisons[0] is comparisons[-1])

    assert run(accelerated) == run(copy.deepcopy) == (7, True)


def test_deep_recursion_guard_does_not_crash() -> None:
    root = []
    current = root
    for _ in range(100000):
        child = []
        current.append(child)
        current = child
    for fn in [copy.deepcopy, accelerated]:
        with pytest.raises(RecursionError):
            fn(root)
