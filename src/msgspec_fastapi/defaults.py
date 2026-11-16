"""Check supported data before implicit copies."""

import copy
import copyreg
import dataclasses
import datetime
import decimal
import enum
import ipaddress
import pathlib
import sys
import types
import typing
import uuid
from copy import deepcopy as _stdlib_deepcopy
from typing import Any, get_args
from zoneinfo import ZoneInfo

import msgspec
from fastapi.datastructures import _Unset

from ._defaults import _Copier, _Guard
from .responses import _JSON_ENCODER
from .validation import _CLASS_DICT, _CLASS_MRO, _MODEL_BRANDS, _REQUIRED_DEFAULT

_NONE_TYPE = type(None)
_ENUM_NAMESPACE = _CLASS_DICT(enum.Enum)['__dict__']
_TRUSTED_UNION_REDUCER = getattr(copyreg, 'pickle_union', None)

_ATOMIC_IDS = {
    id(cls)
    for cls in (
        type(None),
        bool,
        int,
        float,
        str,
        bytes,
        bytearray,
        memoryview,
        datetime.datetime,
        datetime.date,
        datetime.time,
        datetime.timedelta,
        datetime.timezone,
        ZoneInfo,
        decimal.Decimal,
        uuid.UUID,
        msgspec.Raw,
        type(msgspec.UNSET),
        pathlib.Path,
        pathlib.PosixPath,
        pathlib.WindowsPath,
        pathlib.PurePath,
        pathlib.PurePosixPath,
        pathlib.PureWindowsPath,
        ipaddress.IPv4Address,
        ipaddress.IPv6Address,
        ipaddress.IPv4Interface,
        ipaddress.IPv6Interface,
        ipaddress.IPv4Network,
        ipaddress.IPv6Network,
    )
}

_TYPE_REFERENCE_IDS = {
    id(cls)
    for cls in (
        types.GenericAlias,
        types.UnionType,
        type(typing.List[Any]),
        type(typing.Union[int, str]),
        type(typing.Annotated[int, 0]),
        type(typing.Literal[0]),
        type(typing.List),
    )
}


_MISSING_STATE_METHOD = object()
_STATE_NAMES = (
    '__getstate__',
    '__setstate__',
    '__reduce__',
    '__reduce_ex__',
    '__getnewargs__',
    '__getnewargs_ex__',
    '__getattribute__',
    '__getattr__',
)
_STATE_KEYS = frozenset(_STATE_NAMES)

_NATIVE_STATE_METHODS = tuple(
    (
        name,
        tuple(
            mapping[name]
            for root in (msgspec.Struct, enum.Enum, int, str)
            for mapping in map(_CLASS_DICT, _CLASS_MRO(root))
            if name in mapping
        )
        + ((_MISSING_STATE_METHOD,))
        + (
            (getattr(dataclasses, '_dataclass_getstate'),)
            if name == '__getstate__'
            else (getattr(dataclasses, '_dataclass_setstate'),)
            if name == '__setstate__'
            else ()
        ),
    )
    for name in _STATE_NAMES
)


def _checked_namespace(item: Any, cls: type[Any], namespace: Any) -> dict[str, Any]:
    state = namespace.__get__(item, cls)
    if type(state) is not dict or any(type(key) is not str for key in state):
        raise TypeError('Unsupported instance state in copied data')
    if not _STATE_KEYS.isdisjoint(state):
        raise TypeError('Instance state methods are not supported in copied data')
    copier = state.get('__deepcopy__', _MISSING_STATE_METHOD)
    if copier is not _MISSING_STATE_METHOD and type(copier) is not types.FunctionType:
        raise TypeError('Instance copy methods must be ordinary Python functions')
    return state


def _check_copy_graph_python(value: Any) -> None:
    pending = [value]
    seen: set[int] = set()
    classes: dict[int, tuple[str, Any, tuple[Any, ...]]] = {}
    reducer_ids = {
        id(cls)
        for cls, reducer in copyreg.dispatch_table.items()
        if not (cls is types.UnionType and reducer is _TRUSTED_UNION_REDUCER)
    }
    while pending:
        item = pending.pop()
        cls = type(item)
        if (
            cls is str
            or cls is int
            or cls is bool
            or cls is float
            or cls is bytes
            or cls is _NONE_TYPE
        ):
            continue
        oid = id(item)
        if oid in seen:
            continue
        seen.add(oid)
        if cls is list or cls is tuple or cls is set or cls is frozenset:
            if (cls is set or cls is frozenset) and id(cls) in reducer_ids:
                raise TypeError('Registered copy reducers are not supported in copied data')
            pending.extend(item)
            continue
        if cls is dict:
            for key, child in item.items():
                pending.append(key)
                pending.append(child)
            continue
        cid = id(cls)
        info = classes.get(cid)
        if info is None:
            if (
                issubclass(cls, type)
                or cls is types.FunctionType
                or cls is types.BuiltinFunctionType
            ):
                continue
            if cid in _TYPE_REFERENCE_IDS:
                if cid in reducer_ids:
                    raise TypeError('Registered copy reducers are not supported in copied data')
                pending.extend(get_args(item))
                continue
            info = _copy_class_info(cls)
            classes[cid] = info
        kind, namespace, slots = info
        if cls is datetime.datetime or cls is datetime.time:
            pending.append(item.tzinfo)
        elif cls is datetime.timezone:
            pending.extend((item.utcoffset(None), item.tzname(None)))
        elif cls is ZoneInfo:
            pending.append(item.key)
        if namespace is not None:
            state = _checked_namespace(item, cls, namespace)
            if kind == 'state':
                pending.extend(state.values())
        if cid in reducer_ids and getattr(item, '__deepcopy__', None) is None:
            raise TypeError('Registered copy reducers are not supported in copied data')
        if kind == 'state':
            for descriptor in slots:
                try:
                    child = descriptor.__get__(item, cls)
                except AttributeError:
                    continue
                child_cls = type(child)
                if (
                    child_cls is str
                    or child_cls is int
                    or child_cls is bool
                    or child_cls is float
                    or child_cls is bytes
                    or child_cls is _NONE_TYPE
                ):
                    continue
                pending.append(child)


_NATIVE_GUARD = _Guard(
    fallback=_check_copy_graph_python,
    registry=copyreg.dispatch_table,
    trusted_union_reducer=_TRUSTED_UNION_REDUCER,
    union_type=types.UnionType,
    function_type=types.FunctionType,
    builtin_function_type=types.BuiltinFunctionType,
    getset_descriptor_type=types.GetSetDescriptorType,
    member_descriptor_type=types.MemberDescriptorType,
    method_descriptor_type=types.MethodDescriptorType,
    wrapper_descriptor_type=types.WrapperDescriptorType,
    struct_type=msgspec.Struct,
    enum_type=enum.Enum,
    atomic_ids=_ATOMIC_IDS,
    reference_ids=_TYPE_REFERENCE_IDS,
    model_brands=_MODEL_BRANDS,
    native_state_methods=_NATIVE_STATE_METHODS,
    state_names=_STATE_NAMES,
    enum_namespace=_ENUM_NAMESPACE,
    missing=_MISSING_STATE_METHOD,
    mro_descriptor=type.__dict__['__mro__'],
    dict_descriptor=type.__dict__['__dict__'],
    name_descriptor=type.__dict__['__name__'],
)


deepcopy = _Copier(
    copy_module=copy,
    deepcopy=_stdlib_deepcopy,
    list_copier=getattr(copy, '_deepcopy_list'),
    dict_copier=getattr(copy, '_deepcopy_dict'),
    tuple_copier=getattr(copy, '_deepcopy_tuple'),
    atomic_copier=getattr(copy, '_deepcopy_atomic', None),
    reconstruct=getattr(copy, '_reconstruct'),
    atomic_first=sys.version_info >= (3, 14),
    memo_sentinel=getattr(_stdlib_deepcopy, '__defaults__')[-1],
)


_copy_class_info = _NATIVE_GUARD.class_info
_check_copy_graph = _NATIVE_GUARD.check


def _immutable_value(value: Any) -> bool:
    cls = type(value)
    return (
        cls is _NONE_TYPE or cls is bool or cls is int or cls is float or cls is str or cls is bytes
    )


def check_body_default(value: Any) -> None:
    if _immutable_value(value):
        return
    _check_copy_graph(value)
    _JSON_ENCODER.encode(value)


def copy_body_default(value: Any) -> Any:
    if _immutable_value(value):
        return value
    _check_copy_graph(value)
    _JSON_ENCODER.encode(value)
    result = deepcopy(value)
    # A user-defined copy hook can return a different type; do not pass a branded
    # result into FastAPI's subsequent copy of a field default.
    _check_copy_graph(result)
    return result


def copy_metadata(value: Any) -> Any:
    _check_copy_graph(value)
    result = deepcopy(value)
    _check_copy_graph(result)
    return result


_FIELD_METADATA = (
    'title',
    'description',
    'examples',
    'example',
    'openapi_examples',
    'json_schema_extra',
    'deprecated',
)


def check_field_metadata(info: Any) -> None:
    for name in _FIELD_METADATA:
        value = getattr(info, name, None)
        if name == 'json_schema_extra' and callable(value):
            continue
        if value is not None and value is not _Unset:
            _check_copy_graph(value)


def copy_field_metadata(info: Any) -> None:
    for name in _FIELD_METADATA:
        value = getattr(info, name, None)
        if name == 'json_schema_extra' and callable(value):
            continue
        if value is not None and value is not _Unset:
            setattr(info, name, copy_metadata(value))


class _FieldDefault:
    __slots__ = ('info',)

    def __init__(self, info: Any) -> None:
        self.info = info

    def __call__(self) -> Any:
        info = self.info
        if info.default_factory is not None:
            value = info.default_factory()
            check_body_default(value)
            return value
        if info.default is _REQUIRED_DEFAULT:
            return _REQUIRED_DEFAULT
        return copy_body_default(info.default)

    def __copy__(self) -> '_FieldDefault':
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> '_FieldDefault':
        return self


def guard_field_default(field: Any) -> None:
    info = field.field_info
    if info.default is not _REQUIRED_DEFAULT:
        check_body_default(info.default)
    field.get_default = _FieldDefault(info)
