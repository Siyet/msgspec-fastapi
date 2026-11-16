"""Copy protocols preserve native state operations and argument exceptions."""

import copy
import dataclasses
import weakref
from typing import Any

import msgspec
import pytest

from msgspec_fastapi.defaults import deepcopy as native


def state_case(operation: Any, kind: Any) -> Any:
    events = []
    original_dict_copier = copy._deepcopy_dispatch[dict]
    raw_state = {'value': 7}

    class Slots:
        def items(self) -> Any:
            events.append('items')
            if kind == 'bad-pair':
                return [[1]]
            return [['extra', 9]]

    class TupleState(tuple):
        def __len__(self) -> Any:
            events.append('len')
            return 2

        def __iter__(self) -> Any:
            events.append('iter')
            return iter(({'value': 8}, Slots()))

    class Namespace:
        def update(self, state: Any) -> None:
            events.append(('update', dict(state)))

    class Product:
        def __setattr__(self, key: Any, value: Any) -> None:
            events.append(('setattr', key, value))
            object.__setattr__(self, key, value)

    if kind.startswith('setstate'):

        def getter(self: Any) -> Any:
            events.append('getter')
            if kind == 'setstate-valueerror':
                raise ValueError('probe')
            if kind == 'setstate-none':
                return None

            def apply(state: Any) -> Any:
                events.append(('setstate', state))

            return apply

        Product.__setstate__ = property(getter)
    if kind == 'namespace':

        def namespace(self: Any) -> Any:
            events.append('namespace')
            return Namespace()

        class NamespaceProduct(Product):
            __dict__ = property(namespace)

        Product = NamespaceProduct

    def state_copier(value: Any, memo: Any) -> Any:
        if value is not raw_state:
            return original_dict_copier(value, memo)
        events.append('state-copy')
        if kind in ('none', 'setstate-copy-none'):
            return None
        if kind == 'tuple-subclass':
            return TupleState((1, 2))
        if kind == 'bad-pair':
            return (None, Slots())
        return {'value': 7}

    def constructor() -> Any:
        events.append('constructor')
        copy._deepcopy_dispatch[dict] = state_copier
        return Product()

    @dataclasses.dataclass
    class Original:
        value: int

        def __reduce_ex__(self, protocol: Any) -> Any:
            events.append(('reduce', protocol))
            return (constructor, (), raw_state, None, None)

    try:
        result = operation(Original(7))
        if kind != 'namespace':
            result = dict(object.__getattribute__(result, '__dict__'))
        else:
            result = None
        return (result, events)
    except Exception as error:
        return ((type(error).__name__, str(error)), events)
    finally:
        copy._deepcopy_dispatch[dict] = original_dict_copier


@pytest.mark.parametrize(
    ('kind', 'expected_result'),
    [
        ('none', {}),
        ('tuple-subclass', {'value': 8, 'extra': 9}),
        ('bad-pair', ValueError),
        ('setstate-copy-none', {}),
        ('setstate-none', TypeError),
        ('setstate-valueerror', ValueError),
        ('namespace', None),
    ],
)
def test_dynamic_state_matches_stdlib(kind: Any, expected_result: Any) -> None:
    expected = state_case(copy.deepcopy, kind)
    if isinstance(expected_result, type):
        assert expected[0][0] == expected_result.__name__
    else:
        assert expected[0] == expected_result
    assert state_case(native, kind) == expected


def test_probe_result_release_precedes_second_setstate_getter() -> None:

    def run(operation: Any) -> Any:
        events = []

        class Probe:
            def __del__(self) -> None:
                events.append('probe-release')
                Product.__setstate__ = lambda self, value: events.append(('new-setstate', value))

        class Product:
            @property
            def __setstate__(self) -> Any:
                events.append('getter')
                return Probe()

        @dataclasses.dataclass
        class Original:
            value: int

            def __reduce_ex__(self, protocol: Any) -> Any:
                return (Product, (), {'value': 7})

        operation(Original(7))
        return events

    assert (
        run(native)
        == run(copy.deepcopy)
        == ['getter', 'probe-release', ('new-setstate', {'value': 7})]
    )


def test_constructor_uses_snapshot_arguments_before_state() -> None:

    def run(operation: Any) -> Any:
        events = []
        original_state = {'value': 7}

        @dataclasses.dataclass
        class Original:
            value: int

            def __reduce_ex__(self, protocol: Any) -> Any:
                return (create, (self.value,), original_state)

        class Result:
            pass

        def create(value: Any) -> Any:
            events.append(('constructor', value))
            original_state['value'] = value + 1
            return Result()

        result = operation(Original(7))
        return (result.value, events)

    assert run(native) == run(copy.deepcopy) == (8, [('constructor', 7)])


def test_copied_state_lifetime_precedes_outer_keepalive() -> None:

    def run(operation: Any) -> Any:
        events = []
        original_state = {'value': 7}
        old = copy._deepcopy_dispatch[dict]
        holder = {}

        class State(dict):
            def __del__(self) -> None:
                memo = holder['memo']
                present = any((item is original for item in memo.get(id(memo), [])))
                events.append(('state-release', id(original) in memo, present))

        @dataclasses.dataclass
        class Record:
            value: int

            def __reduce_ex__(self, protocol: Any) -> Any:
                return (Product, (), original_state)

        class Product:
            pass

        original = Record(7)

        def function(value: Any, memo: Any) -> Any:
            holder['memo'] = memo
            copy._deepcopy_dispatch[dict] = old
            return State(value)

        def released(reference: Any) -> Any:
            holder['memo'].pop(id(original_state), None)

        reference = weakref.ref(function, released)
        copy._deepcopy_dispatch[dict] = function
        del function
        try:
            result = operation(original)
            assert reference() is None
            assert result.value == 7
            return events
        finally:
            copy._deepcopy_dispatch[dict] = old

    assert run(native) == run(copy.deepcopy) == [('state-release', True, False)]


def describe(operation: Any, value: Any) -> Any:
    try:
        operation(value)
    except BaseException as error:
        return (
            type(error).__name__,
            str(error),
            type(error.__cause__).__name__,
            str(error.__cause__),
            error.__context__ is error.__cause__,
            error.__suppress_context__,
        )
    raise AssertionError('expected error')


@pytest.mark.parametrize('shape,exception', [(2, StopAsyncIteration), (5, StopIteration)])
def test_constructor_argument_copy_generator_errors(shape: Any, exception: Any) -> None:

    @dataclasses.dataclass
    class Child:
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            raise exception('copy child')

    @dataclasses.dataclass
    class Original:
        child: Child

        def __reduce_ex__(self, protocol: Any) -> Any:
            result = (Product, (self.child,))
            if shape == 5:
                result += ({}, None, None)
            return result

    class Product:
        def __init__(self, value: Any) -> Any:
            self.value = value

    original = Original(Child(7))
    assert describe(native, original) == describe(copy.deepcopy, original)


def test_struct_argument_body_metadata_supported_error() -> None:

    @dataclasses.dataclass
    class Child:
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            raise StopIteration('copy child')

    class Root(msgspec.Struct):
        child: Child

    from msgspec_fastapi import defaults

    original = Root(Child(7))
    expected = describe(copy.deepcopy, original)
    assert describe(native, original) == expected
    assert describe(defaults.copy_body_default, original) == expected
    assert describe(defaults.copy_metadata, original) == expected


@pytest.mark.parametrize('shape', [2, 5])
def test_constructor_itself_stopiteration_is_not_wrapped(shape: Any) -> None:

    def constructor() -> Any:
        raise StopIteration('constructor')

    @dataclasses.dataclass
    class Original:
        value: int

        def __reduce_ex__(self, protocol: Any) -> Any:
            result = (constructor, ())
            if shape == 5:
                result += ({}, None, None)
            return result

    value = Original(7)
    assert describe(native, value) == describe(copy.deepcopy, value)


def test_top_level_hook_stopiteration_is_not_wrapped() -> None:

    @dataclasses.dataclass
    class Original:
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            raise StopIteration('root hook')

    value = Original(7)
    assert describe(native, value) == describe(copy.deepcopy, value)


def test_state_copy_stopiteration_is_not_wrapped() -> None:
    old = copy._deepcopy_dispatch[dict]
    state = {'value': 7}

    def copied(value: Any, memo: Any) -> Any:
        if value is state:
            raise StopIteration('state')
        return old(value, memo)

    class Product:
        pass

    @dataclasses.dataclass
    class Original:
        value: int

        def __reduce_ex__(self, protocol: Any) -> Any:
            return (Product, (), state)

    value = Original(7)
    copy._deepcopy_dispatch[dict] = copied
    try:
        assert describe(native, value) == describe(copy.deepcopy, value)
    finally:
        copy._deepcopy_dispatch[dict] = old
