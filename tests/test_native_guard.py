"""The native guard owns policy references and preserves the reference checks."""

import copyreg
import dataclasses
import gc
import inspect
import types
import weakref
from typing import Any

import msgspec
import pytest

from msgspec_fastapi import defaults
from msgspec_fastapi._defaults import _Guard


def guard_arguments(fallback: Any = defaults._check_copy_graph_python) -> dict[str, Any]:
    return {
        'fallback': fallback,
        'registry': copyreg.dispatch_table,
        'trusted_union_reducer': defaults._TRUSTED_UNION_REDUCER,
        'union_type': types.UnionType,
        'function_type': types.FunctionType,
        'builtin_function_type': types.BuiltinFunctionType,
        'getset_descriptor_type': types.GetSetDescriptorType,
        'member_descriptor_type': types.MemberDescriptorType,
        'method_descriptor_type': types.MethodDescriptorType,
        'wrapper_descriptor_type': types.WrapperDescriptorType,
        'struct_type': msgspec.Struct,
        'enum_type': defaults.enum.Enum,
        'atomic_ids': defaults._ATOMIC_IDS,
        'reference_ids': defaults._TYPE_REFERENCE_IDS,
        'model_brands': defaults._MODEL_BRANDS,
        'native_state_methods': defaults._NATIVE_STATE_METHODS,
        'state_names': defaults._STATE_NAMES,
        'enum_namespace': defaults._ENUM_NAMESPACE,
        'missing': defaults._MISSING_STATE_METHOD,
        'mro_descriptor': type.__dict__['__mro__'],
        'dict_descriptor': type.__dict__['__dict__'],
        'name_descriptor': type.__dict__['__name__'],
    }


def test_native_signatures_match_named_policy_interface() -> None:
    arguments = guard_arguments()
    assert tuple(inspect.signature(_Guard).parameters) == tuple(arguments)
    guard = _Guard(**arguments)
    assert str(inspect.signature(guard.check)) == '(value, /)'
    assert str(inspect.signature(guard.class_info)) == '(cls, /)'
    assert gc.is_tracked(guard)


def test_native_uninitialized_and_reinitialization_are_safe() -> None:
    guard = _Guard.__new__(_Guard)
    for operation in (guard.check, guard.class_info):
        with pytest.raises(TypeError, match='Uninitialized'):
            operation(int)
    bad = {**guard_arguments(), 'model_brands': []}
    with pytest.raises(TypeError):
        guard.__init__(**bad)
    with pytest.raises(TypeError, match='Uninitialized'):
        guard.check(None)
    guard.__init__(**guard_arguments())
    assert guard.check({'items': [1, 2]}) is None
    with pytest.raises(TypeError, match='reinitialized'):
        guard.__init__(**guard_arguments())


def test_native_fallback_globals_cycle_is_collectable() -> None:
    namespace: dict[str, Any] = {}
    exec('def fallback(value):\n    return None\n', namespace)
    reference = weakref.ref(namespace['fallback'])
    guard = _Guard(**guard_arguments(namespace['fallback']))
    namespace['guard'] = guard
    del namespace, guard
    gc.collect()
    assert reference() is None


def test_native_context_retains_and_releases_policy_type() -> None:
    temporary = type('TemporaryPolicyType', (), {})
    reference = weakref.ref(temporary)
    arguments = {**guard_arguments(), 'struct_type': temporary}
    guard = _Guard(**arguments)
    del temporary, arguments
    gc.collect()
    assert reference() is not None
    del guard
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize(
    ('key', 'value'),
    [
        ('fallback', None),
        ('registry', []),
        ('function_type', 1),
        ('atomic_ids', {True}),
        ('reference_ids', [1]),
        ('model_brands', ('valid', 1)),
        ('native_state_methods', (('bad', []),)),
        ('state_names', []),
        ('mro_descriptor', None),
        ('dict_descriptor', None),
        ('name_descriptor', None),
        ('enum_namespace', None),
    ],
)
def test_invalid_native_context_configuration_raises(key: str, value: Any) -> None:
    with pytest.raises(TypeError):
        _Guard(**{**guard_arguments(), key: value})


def test_native_bad_descriptor_result_raises_without_partial_state_leak() -> None:
    arguments = {**guard_arguments(), 'mro_descriptor': type.__dict__['__dict__']}
    guard = _Guard(**arguments)
    for _ in range(10):
        with pytest.raises(TypeError, match='MRO'):
            guard.class_info(int)


def test_native_guard_and_reference_reject_same_graphs() -> None:
    @dataclasses.dataclass
    class Record:
        value: Any

    class Slotted(Record):
        __slots__ = ('extra',)

    value = Slotted({'items': [1, 2]})
    guard = _Guard(**guard_arguments())
    assert guard.check(value) is None
    assert defaults._check_copy_graph_python(value) is None
    shared: list[Any] = []
    cycle = [shared, shared]
    shared.append(cycle)
    assert guard.check(cycle) is None
    assert defaults._check_copy_graph_python(cycle) is None
    value.extra = object()
    for operation in (guard.check, defaults._check_copy_graph_python):
        with pytest.raises(TypeError, match='Unsupported object'):
            operation(value)


def test_native_fallback_error_does_not_retain_data() -> None:
    class Unsupported:
        pass

    value = Unsupported()
    reference = weakref.ref(value)

    def fallback(item: Any) -> None:
        raise ValueError('fallback failure')

    guard = _Guard(**guard_arguments(fallback))
    for _ in range(10):
        # A path is a supported opaque value handled by the Python reference guard.
        from pathlib import Path

        with pytest.raises(ValueError, match='fallback failure'):
            guard.check(Path('/value'))
        with pytest.raises(TypeError, match='Unsupported object'):
            guard.check(value)
    del value
    gc.collect()
    assert reference() is None


def test_reentrant_namespace_initialization_does_not_retain_old_cache() -> None:
    @dataclasses.dataclass
    class Outer:
        value: int

    @dataclasses.dataclass
    class Inner:
        value: int

    class Marker:
        pass

    def namespace(owner: Any) -> Any:
        for item in gc.get_referents(owner):
            if (
                type(item) is dict
                and '__subclasshook__' in item
                and '__reduce_ex__' in item
                and '__doc__' in item
            ):
                return item
        return None

    original_threshold = gc.get_threshold()
    for offset in range(1, 35):
        owner = _Guard(**guard_arguments())
        active = False
        called = False
        watched = []

        def collect(phase: str, info: Any, owner: Any = owner) -> None:
            nonlocal called
            if phase == 'start' and active and not called and namespace(owner) is None:
                called = True
                owner.class_info(Inner)
                value = namespace(owner)
                if value is not None:
                    marker = Marker()
                    watched.append(weakref.ref(marker))
                    value['_gc_lifetime_probe'] = marker

        gc.collect()
        gc.callbacks.append(collect)
        gc.set_threshold(offset, 0, 0)
        active = True
        try:
            owner.class_info(Outer)
        finally:
            active = False
            gc.set_threshold(*original_threshold)
            gc.callbacks.remove(collect)
        del owner, collect
        gc.collect()
        assert all(reference() is None for reference in watched)
