"""Error-only JSON traversal; successful requests retain typed msgspec decoding."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import inspect
import uuid
from collections.abc import Callable
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    NamedTuple,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import msgspec
from typing_extensions import NotRequired, Required, is_typeddict

from .validation import base_type, pattern_validator


class _Unsupported(Exception):
    pass


class _Node(NamedTuple):
    target: Any
    origin: Any
    args: tuple[Any, ...]
    cls: Any
    metadata: tuple[Any, ...]
    check_patterns: Callable[..., list[dict[str, Any]]] | None


_TYPE_MRO = type.__dict__['__mro__'].__get__
_TYPE_DICT = type.__dict__['__dict__'].__get__
_ENUM_METHODS = tuple(
    (name, tuple(inspect.getattr_static(base, name) for base in bases))
    for name, bases in (
        ('_missing_', (enum.Enum,)),
        ('__hash__', (enum.Enum, object, int, str)),
        ('__eq__', (enum.Enum, object, int, str)),
        ('__getattribute__', (enum.Enum, object, int, str)),
    )
)


def _type_guard(watched: list[tuple[type, tuple[str, ...]]]) -> Callable[[], bool]:
    sentinel = object()
    namespaces = []
    mros = []
    seen: set[tuple[int, str]] = set()
    classes: set[int] = set()
    for cls, names in watched:
        mro = _TYPE_MRO(cls)
        if id(cls) not in classes:
            classes.add(id(cls))
            mros.append((cls, mro))
        for base in mro:
            mapping = _TYPE_DICT(base)
            for name in names:
                key = (id(base), name)
                if key not in seen:
                    seen.add(key)
                    namespaces.append((mapping, name, mapping.get(name, sentinel)))

    def unchanged() -> bool:
        return all(_TYPE_MRO(cls) is before for cls, before in mros) and all(
            mapping.get(name, sentinel) is before for mapping, name, before in namespaces
        )

    return unchanged


def _fields(annotation: Any) -> tuple[list[tuple[str | int, Any, bool]], Any]:
    cls = get_origin(annotation) or annotation
    if isinstance(cls, type) and issubclass(cls, msgspec.Struct):
        config = cls.__struct_config__
        offset = int(config.array_like and config.tag is not None)
        fields = [
            (index + offset if config.array_like else field.encode_name, field.type, field.required)
            for index, field in enumerate(msgspec.structs.fields(annotation))
        ]
        return fields, config
    if is_typeddict(cls):
        if get_origin(annotation) is not None:
            raise _Unsupported
        hints = get_type_hints(cls, include_extras=True)
        fields = []
        for name, target in hints.items():
            required = name in cls.__required_keys__
            if get_origin(target) in (Required, NotRequired):
                required = get_origin(target) is Required
                target = get_args(target)[0]
            fields.append((name, target, required))
        return fields, None
    if isinstance(cls, type) and dataclasses.is_dataclass(cls):
        if get_origin(annotation) is not None:
            raise _Unsupported
        hints = get_type_hints(cls, include_extras=True)
        return [
            (
                field.name,
                hints[field.name],
                field.default is dataclasses.MISSING
                and field.default_factory is dataclasses.MISSING,
            )
            for field in dataclasses.fields(cls)
        ], None
    raise _Unsupported


def _safe(annotation: Any, seen: set[Any], field_cache: dict[Any, Any], plain: set[Any]) -> bool:
    """Check hooks and identify types decoded without constructing models."""
    annotation = base_type(annotation)
    origin = get_origin(annotation)
    cls = origin or annotation
    if origin is None and not isinstance(cls, type) and annotation is not Any:
        raise _Unsupported
    if isinstance(cls, type) and issubclass(cls, tuple) and hasattr(cls, '_fields'):
        raise _Unsupported
    if isinstance(cls, type) and (
        issubclass(cls, msgspec.Struct) or dataclasses.is_dataclass(cls) or is_typeddict(cls)
    ):
        if annotation in seen:
            return False
        seen.add(annotation)
        if getattr(cls, '__post_init__', None) is not None:
            raise _Unsupported
        field_cache[annotation] = _fields(annotation)
        fields, _ = field_cache[annotation]
        for _, child, _ in fields:
            _safe(child, seen, field_cache, plain)
        return False
    if origin in (list, tuple, set, frozenset, dict, Union, UnionType):
        children = [
            _safe(arg, seen, field_cache, plain)
            for arg in get_args(annotation)
            if arg is not Ellipsis
        ]
        decodable = all(children)
    elif origin is Literal or annotation is Any:
        decodable = True
    elif isinstance(cls, type) and issubclass(cls, enum.Enum):
        for name, defaults in _ENUM_METHODS:
            method = inspect.getattr_static(cls, name)
            if not any(method is default for default in defaults):
                raise _Unsupported
        decodable = True
    elif cls in (
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
        decimal.Decimal,
        uuid.UUID,
        msgspec.Raw,
    ):
        decodable = True
    else:
        raise _Unsupported
    if decodable:
        plain.add(annotation)
    return decodable


def _error(
    loc: tuple[str | int, ...], message: str, kind: str = 'value_error.msgspec'
) -> dict[str, Any]:
    return {'loc': loc, 'msg': message, 'type': kind}


def _convert_errors(
    value: Any, target: Any, loc: tuple[str | int, ...], nodes: dict[Any, _Node]
) -> list[dict[str, Any]]:
    try:
        msgspec.json.decode(msgspec.json.encode(value), type=target)
    except msgspec.ValidationError as exc:
        return [_error(loc, str(exc))]
    check = nodes[target].check_patterns
    return check(value, loc) if check is not None else []


def _walk(
    value: Any,
    annotation: Any,
    loc: tuple[str | int, ...],
    field_cache: dict[Any, Any],
    plain: set[Any],
    nodes: dict[Any, _Node],
) -> list[dict[str, Any]]:
    target, origin, args, cls, metadata, _ = nodes[annotation]
    if target in plain:
        errors = _convert_errors(value, annotation, loc, nodes)
        if not errors or origin not in (list, tuple, set, frozenset, dict):
            return errors
    if origin is Union or origin is UnionType:
        if Any in args:
            return []
        if value is None and type(None) in args:
            return []
        structured = []
        for child in args:
            child_cls = get_origin(base_type(child)) or base_type(child)
            if isinstance(child_cls, type) and (
                issubclass(child_cls, msgspec.Struct)
                or dataclasses.is_dataclass(child_cls)
                or is_typeddict(child_cls)
            ):
                config = (
                    child_cls.__struct_config__ if issubclass(child_cls, msgspec.Struct) else None
                )
                if isinstance(value, dict) and not (config and config.array_like):
                    structured.append(child)
                elif isinstance(value, list) and config and config.array_like:
                    structured.append(child)
        if len(structured) == 1:
            return _walk(value, structured[0], loc, field_cache, plain, nodes)
        if structured:
            for child in structured:
                config = (get_origin(base_type(child)) or base_type(child)).__struct_config__
                key = 0 if config.array_like else config.tag_field
                if isinstance(value, dict):
                    tag = value.get(key, msgspec.NODEFAULT)
                else:
                    tag = value[0] if value else msgspec.NODEFAULT
                if type(tag) is type(config.tag) and tag == config.tag:
                    return _walk(value, child, loc, field_cache, plain, nodes)
            key = (
                0
                if isinstance(value, list)
                else (
                    get_origin(base_type(structured[0])) or base_type(structured[0])
                ).__struct_config__.tag_field
            )
            return [_error(loc + (key,), 'Invalid or missing type tag')]
        if target not in plain:
            raise _Unsupported
        return _convert_errors(value, annotation, loc, nodes)

    if isinstance(cls, type) and (
        issubclass(cls, msgspec.Struct) or dataclasses.is_dataclass(cls) or is_typeddict(cls)
    ):
        fields, config = field_cache[target]
        array = bool(config and config.array_like)
        if not isinstance(value, list if array else dict):
            if type(value) is int and not -(1 << 63) <= value < (1 << 64):
                return _convert_errors(value, annotation, loc, nodes)
            try:
                msgspec.convert(value, type=annotation)
            except msgspec.ValidationError as exc:
                return [_error(loc, str(exc))]
            raise _Unsupported
        errors = []
        if config and config.tag is not None:
            key = 0 if array else config.tag_field
            present = len(value) > 0 if array else key in value
            if present and (type(value[key]) is not type(config.tag) or value[key] != config.tag):
                errors.append(_error(loc + (key,), 'Invalid type tag'))
        for key, child, required in fields:
            if array:
                assert isinstance(value, list) and isinstance(key, int)
                present = key < len(value)
            else:
                assert isinstance(value, dict)
                present = key in value
            if present:
                errors.extend(_walk(value[key], child, loc + (key,), field_cache, plain, nodes))
            elif required:
                errors.append(_error(loc + (key,), 'Field required', 'missing'))
        if config and config.forbid_unknown_fields:
            known = {key for key, _, _ in fields}
            if config.tag is not None:
                known.add(0 if array else config.tag_field)
            for key in range(len(value)) if array else value:
                if key not in known:
                    errors.append(_error(loc + (key,), 'Unexpected field', 'extra_forbidden'))
        return errors

    if origin in (list, tuple, set, frozenset, dict):
        if target in (tuple[()], Tuple[()]):
            raise _Unsupported
        if (
            origin in (set, frozenset)
            or (origin is dict and args and base_type(args[0]) not in (str, Any))
        ) and any(
            isinstance(item, msgspec.Meta)
            and (item.min_length is not None or item.max_length is not None)
            for item in metadata
        ):
            raise _Unsupported
        if not isinstance(value, dict if origin is dict else list):
            return _convert_errors(value, annotation, loc, nodes)
        relaxed: Any
        if origin is dict:
            relaxed = dict[Any, Any]
        elif origin is tuple and args and args[-1] is not Ellipsis:
            relaxed = origin[tuple(Any for _ in args)]
        else:
            relaxed = origin[Any] if origin is not tuple else tuple[Any, ...]
        if metadata:
            relaxed = Annotated[relaxed, *metadata]
        errors = []
        try:
            msgspec.convert(value, type=relaxed)
        except msgspec.ValidationError as exc:
            errors.append(_error(loc, str(exc)))
        except TypeError as exc:
            raise _Unsupported from exc
        if origin is dict:
            assert isinstance(value, dict)
            key_type, child_type = args or (Any, Any)
            for key, item in value.items():
                try:
                    msgspec.json.decode(
                        msgspec.json.encode({key: None}), type=origin[key_type, Any]
                    )
                except msgspec.ValidationError as exc:
                    errors.append(_error(loc + (key, '[key]'), str(exc)))
                check = nodes[key_type].check_patterns
                if check is not None:
                    errors.extend(check(key, loc + (key, '[key]')))
                errors.extend(_walk(item, child_type, loc + (key,), field_cache, plain, nodes))
        else:
            for index, item in enumerate(value):
                if origin is tuple and args and args[-1] is not Ellipsis:
                    if index >= len(args):
                        continue
                    child = args[index]
                else:
                    child = args[0] if args else Any
                errors.extend(_walk(item, child, loc + (index,), field_cache, plain, nodes))
        return errors
    return _convert_errors(value, annotation, loc, nodes)


def compile_body_errors(annotation: Any) -> Callable[..., list[dict[str, Any]]]:
    """Prepare immutable schema metadata; keep error lists local to each call."""
    try:
        field_cache: dict[Any, Any] = {}
        plain: set[Any] = set()
        _safe(annotation, set(), field_cache, plain)
        nodes: dict[Any, _Node] = {}
        pending = [annotation]
        while pending:
            current = pending.pop()
            if current in nodes:
                continue
            target = base_type(current)
            origin, args = get_origin(target), get_args(target)
            metadata = get_args(current)[1:] if get_origin(current) is Annotated else ()
            nodes[current] = _Node(
                target, origin, args, origin or target, metadata, pattern_validator(current)
            )
            if target in field_cache:
                pending.extend(child for _, child, _ in field_cache[target][0])
            if origin is not Literal:
                pending.extend(arg for arg in args if arg is not Ellipsis)
        watched: list[tuple[type, tuple[str, ...]]] = []
        for node in nodes.values():
            if node.target in field_cache:
                watched.append((node.cls, ('__post_init__',)))
            elif isinstance(node.cls, type) and issubclass(node.cls, enum.Enum):
                watched.append((node.cls, tuple(name for name, _ in _ENUM_METHODS)))
        unchanged = _type_guard(watched)
    except (_Unsupported, TypeError, NameError, RecursionError):
        return lambda value, loc=('body',): []

    def diagnose(value: Any, loc: tuple[str | int, ...] = ('body',)) -> list[dict[str, Any]]:
        if not unchanged():
            return []
        try:
            return _walk(value, annotation, loc, field_cache, plain, nodes)
        except (_Unsupported, TypeError, NameError, RecursionError):
            return []

    return diagnose


def body_errors(
    value: Any, annotation: Any, loc: tuple[str | int, ...] = ('body',)
) -> list[dict[str, Any]]:
    return compile_body_errors(annotation)(value, loc)


def _raw_pure(annotation: Any) -> bool:
    if get_origin(annotation) is Annotated:
        if any(not isinstance(m, msgspec.Meta) for m in get_args(annotation)[1:]):
            return False
        annotation = get_args(annotation)[0]
    if any(annotation is t for t in (str, int, float, bool, type(None))):
        return True
    origin, args = get_origin(annotation), get_args(annotation)
    if any(origin is t for t in (list, set, frozenset)):
        return len(args) == 1 and _raw_pure(args[0])
    if origin is dict:
        return len(args) == 2 and all(_raw_pure(a) for a in args)
    if origin is tuple:
        return bool(args) and all(_raw_pure(a) for a in args if a is not Ellipsis)
    if origin is Union or origin is UnionType:
        return bool(args) and all(_raw_pure(a) for a in args)
    return False


def _compile_raw_list_errors(
    annotation: Any, full_walk: Callable[..., list[dict[str, Any]]]
) -> Callable[..., list[dict[str, Any]]] | None:
    root = base_type(annotation)
    args = get_args(root)
    if get_origin(root) is not list or len(args) != 1 or pattern_validator(annotation) is not None:
        return None
    child = args[0]
    cls = get_origin(base_type(child)) or base_type(child)
    if not isinstance(cls, type) or not issubclass(cls, msgspec.Struct):
        return None
    try:
        _safe(annotation, set(), {}, set())
        fields = msgspec.structs.fields(child)
        if not all(_raw_pure(f.type) for f in fields):
            return None
        config = cls.__struct_config__
        specs = [
            (f'_field_{index}', f.type, msgspec.field(name=f.encode_name))
            if f.required
            else (f'_field_{index}', f.type, msgspec.field(name=f.encode_name, default=None))
            for index, f in enumerate(fields)
        ]
        # JSON-only fields and inert defaults cannot form cycles in this transient model.
        wire = msgspec.defstruct(
            '_DiagnosticWire',
            specs,
            kw_only=True,
            gc=False,
            tag=config.tag,
            tag_field=config.tag_field or 'type',
            array_like=config.array_like,
            forbid_unknown_fields=True,
        )
        wire_decoder = msgspec.json.Decoder(type=wire)
        raw_decoder = msgspec.json.Decoder(type=list[msgspec.Raw])
        any_decoder = msgspec.json.Decoder()
        child_walk = compile_body_errors(child)
        metadata = get_args(annotation)[1:] if get_origin(annotation) is Annotated else ()
        relaxed = Annotated[list[Any], *metadata] if metadata else None
        unchanged = _type_guard([(cls, ('__post_init__',))])
        decode_piece = wire_decoder.decode
    except (_Unsupported, TypeError, NameError, RecursionError):
        return None

    def diagnose(raw: bytes, loc: tuple[str | int, ...] = ('body',)) -> list[dict[str, Any]]:
        if not unchanged() or (not config.array_like and b'{' not in raw):
            return full_walk(any_decoder.decode(raw), loc)
        try:
            pieces = raw_decoder.decode(raw)
        except (msgspec.DecodeError, UnicodeError):
            return full_walk(any_decoder.decode(raw), loc)
        errors = []
        if relaxed is not None:
            try:
                msgspec.convert(pieces, type=relaxed)
            except msgspec.ValidationError as exc:
                errors.append(_error(loc, str(exc)))
        for index, piece in enumerate(pieces):
            try:
                decode_piece(piece)
            except (msgspec.DecodeError, UnicodeError):
                # Validate complete failed/unknown pieces. Full fallback preserves
                # original syntax-error text and first-invalid-value ordering.
                try:
                    value = any_decoder.decode(piece)
                except (msgspec.DecodeError, UnicodeError):
                    return full_walk(any_decoder.decode(raw), loc)
                errors.extend(child_walk(value, loc + (index,)))
        return errors

    return diagnose


def compile_json_body_errors(annotation: Any) -> Callable[..., list[dict[str, Any]]]:
    """Use original JSON slices when safe, otherwise retain the structural traversal."""
    walk = compile_body_errors(annotation)
    raw_walk = _compile_raw_list_errors(annotation, walk)
    if raw_walk is not None:
        return raw_walk

    def diagnose(raw: bytes, loc: tuple[str | int, ...] = ('body',)) -> list[dict[str, Any]]:
        return walk(msgspec.json.decode(raw), loc)

    return diagnose
