"""Route-local validation for HTTP parameters; no global FastAPI changes."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import is_dataclass
from types import UnionType
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin, get_type_hints

import annotated_types
import msgspec
from fastapi.dependencies.models import Dependant
from fastapi.params import Body, Param
from typing_extensions import is_typeddict

_REQUIRED_DEFAULT = inspect.signature(Body).parameters['default'].default


class _Pattern:
    def __init__(self, pattern: str | re.Pattern[str]) -> None:
        if not isinstance(pattern, str):
            raise TypeError('Use a string pattern; compiled Python patterns are not supported')
        from regex_rs import Regex

        self.pattern = pattern
        self.regex = Regex(pattern)


def pattern_validator(
    annotation: Any, *, strings_validated: bool = False
) -> Callable[..., list[dict[str, Any]]] | None:
    """Compile checks for translated FastAPI patterns, preserving raw container positions."""
    metadata = get_args(annotation)[1:] if get_origin(annotation) is Annotated else ()
    patterns = [item for item in metadata if isinstance(item, _Pattern)]
    annotation = base_type(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    children = []
    if origin in (list, set, frozenset, tuple, dict, Union, UnionType):
        children = [
            None if arg is Ellipsis else pattern_validator(arg, strings_validated=strings_validated)
            for arg in args
        ]
    if not patterns and not any(children):
        return None
    if (
        strings_validated
        and not patterns
        and (origin is list or (origin is tuple and len(args) == 2 and args[1] is Ellipsis))
    ):
        child = args[0]
        child_metadata = get_args(child)[1:] if get_origin(child) is Annotated else ()
        child_patterns = [item for item in child_metadata if isinstance(item, _Pattern)]
        if base_type(child) is str and len(child_patterns) == 1:
            pattern = child_patterns[0]
            matches = pattern.regex.is_match
            message = f"String should match pattern '{pattern.pattern}'"

            def check_strings(value: Any, loc: tuple[str | int, ...] = ()) -> list[dict[str, Any]]:
                return [
                    {
                        'loc': loc + (index,),
                        'msg': message,
                        'type': 'string_pattern_mismatch',
                        'ctx': {'pattern': pattern.pattern},
                    }
                    for index, matched in enumerate(map(matches, value))
                    if not matched
                ]

            return check_strings

    def check(value: Any, loc: tuple[str | int, ...] = ()) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for pattern in patterns:
            if not isinstance(value, str):
                errors.append(
                    {'loc': loc, 'msg': 'Input should be a valid string', 'type': 'string_type'}
                )
            elif not pattern.regex.is_match(value):
                errors.append(
                    {
                        'loc': loc,
                        'msg': f"String should match pattern '{pattern.pattern}'",
                        'type': 'string_pattern_mismatch',
                        'ctx': {'pattern': pattern.pattern},
                    }
                )
        if origin in (list, set, frozenset, tuple) and isinstance(
            value, (list, tuple, set, frozenset)
        ):
            for index, item in enumerate(value):
                child = (
                    children[index]
                    if origin is tuple and args[-1] is not Ellipsis and index < len(children)
                    else children[0]
                )
                if child is not None:
                    errors.extend(child(item, loc + (index,)))
        elif origin is dict and isinstance(value, dict):
            key_check, value_check = children
            for key, item in value.items():
                if key_check is not None:
                    errors.extend(key_check(key, loc + (key, '[key]')))
                if value_check is not None:
                    errors.extend(value_check(item, loc + (key,)))
        elif origin in (Union, UnionType) and Any not in args:
            branch_errors: list[dict[str, Any]] = []
            for arg, child in zip(args, children):
                target = get_origin(base_type(arg)) or base_type(arg)
                if target is Literal:
                    try:
                        msgspec.convert(value, type=arg)
                    except msgspec.ValidationError:
                        continue
                    break
                if not isinstance(target, type) or not isinstance(value, target):
                    continue
                # Check scalar constraints without constructing models or repeating hooks.
                if target in (str, int, float, bool, bytes):
                    try:
                        msgspec.convert(value, type=arg)
                    except msgspec.ValidationError:
                        continue
                current = child(value, loc) if child is not None else []
                if not current:
                    break
                if not branch_errors:
                    branch_errors = current
            else:
                errors.extend(branch_errors)
        return errors

    return check


def base_type(annotation: Any) -> Any:
    return get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation


def patterns_need_raw(annotation: Any) -> bool:
    """Keep raw positions except for strict string, list and tuple trees."""
    metadata = get_args(annotation)[1:] if get_origin(annotation) is Annotated else ()
    target = base_type(annotation)
    if target is str:
        return False
    if get_origin(target) in (list, tuple) and not any(
        isinstance(item, _Pattern) for item in metadata
    ):
        return any(patterns_need_raw(arg) for arg in get_args(target) if arg is not Ellipsis)
    return True


def structured_type(annotation: Any) -> bool:
    value = get_origin(base_type(annotation)) or base_type(annotation)
    return isinstance(value, type) and (
        issubclass(value, msgspec.Struct)
        or is_typeddict(value)
        or is_dataclass(value)
        or (issubclass(value, tuple) and hasattr(value, '_fields'))
    )


def _check_structured_metadata(annotation: Any, seen: set[Any] | None = None) -> None:
    if seen is None:
        seen = set()
    if get_origin(annotation) is Annotated:
        annotation, *metadata = get_args(annotation)
        if any(not isinstance(item, msgspec.Meta) for item in metadata):
            raise TypeError('Use msgspec.Meta inside structured model fields')
    origin = get_origin(annotation)
    value = origin or annotation
    if isinstance(value, type):
        _assert_unbranded(value)
    structured = structured_type(annotation)
    if structured and value not in seen:
        seen.add(value)
        for field_type in get_type_hints(value, include_extras=True).values():
            _check_structured_metadata(field_type, seen)
    for arg in get_args(annotation):
        _check_structured_metadata(arg, seen)


def constrained_type(annotation: Any, metadata: Iterable[Any] = ()) -> tuple[Any, bool | None]:
    """Translate supported FastAPI constraints, rejecting unhandled validation."""
    if get_origin(annotation) is Annotated:
        annotation, *extras = get_args(annotation)
        metadata = (*extras, *metadata)
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(origin or annotation, type):
        _assert_unbranded(origin or annotation)
    if (
        (type(metadata) is tuple or type(metadata) is list)
        and not metadata
        and (
            annotation is int
            or annotation is str
            or annotation is float
            or annotation is bool
            or annotation is bytes
        )
    ):
        return annotation, None
    if structured_type(annotation):
        _check_structured_metadata(annotation)
    if args and origin is not Literal:
        converted: list[Any] = []
        for arg in args:
            if arg is Ellipsis:
                converted.append(arg)
                continue
            target, nested_strict = constrained_type(arg)
            if nested_strict is not None:
                raise TypeError(
                    'Nested strict constraints are not supported; use a strict top-level parameter'
                )
            converted.append(target)
        converted_args = tuple(converted)
        if hasattr(annotation, 'copy_with'):
            annotation = annotation.copy_with(converted_args)
        elif origin in (Union, UnionType):
            annotation = Union[converted_args]
        else:
            annotation = origin[converted_args[0] if len(converted_args) == 1 else converted_args]
    expanded = []
    for item in metadata:
        if isinstance(item, (Param, Body)):
            expanded.extend(item.metadata)
        elif type(item).__module__ == 'pydantic.fields' and type(item).__name__ == 'FieldInfo':
            # Nested Field supports constraint metadata only, not model field options.
            if item.default is not _REQUIRED_DEFAULT or item.default_factory is not None:
                raise TypeError('Nested Field defaults are not supported')
            for name in (
                'alias',
                'alias_priority',
                'validation_alias',
                'serialization_alias',
                'annotation',
                'discriminator',
                'exclude',
                'exclude_if',
                'frozen',
                'validate_default',
                'init',
                'init_var',
                'kw_only',
                'title',
                'field_title_generator',
                'description',
                'examples',
                'deprecated',
                'json_schema_extra',
            ):
                if getattr(item, name, None) is not None:
                    raise TypeError(f'Unsupported nested Field option: {name}')
            if item.repr is not True:
                raise TypeError('Unsupported nested Field option: repr')
            attributes = item._attributes_set
            if type(attributes) is not dict or any(type(key) is not str for key in attributes):
                raise TypeError('Unsupported nested Field attributes')
            for name, value in attributes.items():
                if name == 'default' and value is _REQUIRED_DEFAULT:
                    continue
                raise TypeError(f'Unsupported nested Field option: {name}')
            if type(item.metadata) is not list:
                raise TypeError('Unsupported nested Field metadata')
            expanded.extend(item.metadata)
        else:
            expanded.append(item)
    metadata = expanded
    constraints: dict[str, Any] = {}
    metas = []
    strict = None
    for item in metadata:
        if isinstance(item, (msgspec.Meta, _Pattern)):
            metas.append(item)
        elif isinstance(
            item,
            (
                annotated_types.Gt,
                annotated_types.Ge,
                annotated_types.Lt,
                annotated_types.Le,
                annotated_types.MultipleOf,
                annotated_types.MinLen,
                annotated_types.MaxLen,
            ),
        ):
            name = {'MinLen': 'min_length', 'MaxLen': 'max_length'}.get(
                type(item).__name__,
                {'MultipleOf': 'multiple_of'}.get(type(item).__name__, type(item).__name__.lower()),
            )
            constraints[name] = getattr(item, name)
        elif type(item).__module__ == 'pydantic.types' and type(item).__name__ == 'Strict':
            if type(item.strict) is not bool:
                raise TypeError('Strict metadata must contain a bool')
            strict = item.strict
        elif (
            type(item).__module__ == 'pydantic._internal._fields'
            and type(item).__name__ == '_PydanticGeneralMetadata'
        ):
            values = vars(item)
            if type(values) is not dict or any(type(key) is not str for key in values):
                raise TypeError('Unsupported constraint metadata')
            for name, value in values.items():
                if name not in ('pattern',):
                    raise TypeError(f'Unsupported msgspec parameter constraint: {name}')
                constraints[name] = value
        else:
            raise TypeError(f'Unsupported msgspec parameter metadata: {type(item).__name__}')
    if 'pattern' in constraints:
        pattern = _Pattern(constraints.pop('pattern'))
        metas.extend((pattern, msgspec.Meta(extra_json_schema={'pattern': pattern.pattern})))
    if constraints:
        metas.append(msgspec.Meta(**constraints))
    if metas:
        # FastAPI constraints on nullable scalars apply to the non-null branch.
        args = get_args(annotation)
        if get_origin(annotation) in (Union, UnionType) and type(None) in args and len(args) == 2:
            inner = next(arg for arg in args if arg is not type(None))
            target = Union[Annotated[inner, *metas], type(None)]
        else:
            target = Annotated[annotation, *metas]
    else:
        target = annotation
    return target, strict


_CLASS_MRO = type.__dict__['__mro__'].__get__
_CLASS_DICT = type.__dict__['__dict__'].__get__
_MODEL_BRANDS = (
    '__pydantic_validator__',
    '__pydantic_serializer__',
    '__get_pydantic_core_schema__',
    '__pydantic_config__',
    '__pydantic_model__',
    '__get_validators__',
)


def _assert_unbranded(cls: Any) -> None:
    if cls is int or cls is str or cls is float or cls is bool or cls is bytes:
        return
    if not isinstance(cls, type):
        cls = get_origin(cls)
    if not isinstance(cls, type):
        raise TypeError('Unsupported model class')
    for base in _CLASS_MRO(cls):
        namespace = _CLASS_DICT(base)
        try:
            for name in _MODEL_BRANDS:
                if name in namespace:
                    raise TypeError('Pydantic model behavior is not supported for msgspec data')
        except StopIteration as exc:
            raise RuntimeError('generator raised StopIteration') from exc


_LEAVES = (
    msgspec.inspect.AnyType,
    msgspec.inspect.NoneType,
    msgspec.inspect.BoolType,
    msgspec.inspect.IntType,
    msgspec.inspect.FloatType,
    msgspec.inspect.StrType,
    msgspec.inspect.BytesType,
    msgspec.inspect.ByteArrayType,
    msgspec.inspect.MemoryViewType,
    msgspec.inspect.DateTimeType,
    msgspec.inspect.TimeType,
    msgspec.inspect.DateType,
    msgspec.inspect.TimeDeltaType,
    msgspec.inspect.UUIDType,
    msgspec.inspect.DecimalType,
    msgspec.inspect.ExtType,
    msgspec.inspect.RawType,
    msgspec.inspect.EnumType,
    msgspec.inspect.LiteralType,
)


def ensure_native_type(annotation: Any) -> None:
    if (
        annotation is int
        or annotation is str
        or annotation is float
        or annotation is bool
        or annotation is bytes
    ):
        return
    direct = get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
    if isinstance(direct, type):
        _assert_unbranded(direct)
    pending = [msgspec.inspect.type_info(annotation)]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, msgspec.inspect.CustomType):
            raise TypeError('Custom decoding hooks are not supported for typed msgspec data')
        if isinstance(
            node,
            (
                msgspec.inspect.StructType,
                msgspec.inspect.DataclassType,
                msgspec.inspect.TypedDictType,
                msgspec.inspect.NamedTupleType,
                msgspec.inspect.EnumType,
            ),
        ):
            _assert_unbranded(node.cls)
        if isinstance(node, msgspec.inspect.Metadata):
            pending.append(node.type)
        elif isinstance(node, msgspec.inspect.CollectionType):
            pending.append(node.item_type)
        elif isinstance(node, msgspec.inspect.TupleType):
            pending.extend(node.item_types)
        elif isinstance(node, msgspec.inspect.UnionType):
            pending.extend(node.types)
        elif isinstance(node, (msgspec.inspect.DictType, msgspec.inspect.FrozenDictType)):
            pending.extend((node.key_type, node.value_type))
        elif isinstance(
            node,
            (
                msgspec.inspect.StructType,
                msgspec.inspect.DataclassType,
                msgspec.inspect.TypedDictType,
                msgspec.inspect.NamedTupleType,
            ),
        ):
            pending.extend(field.type for field in node.fields)
        elif not isinstance(node, _LEAVES):
            raise TypeError('Unsupported msgspec type information')


# Unicode White_Space used by Rust str::trim, excluding Python's extra C0 separators.
_HTTP_WHITESPACE = '\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000'
_HTTP_INTEGER = re.compile(r'-?(?:0|[1-9][0-9]*)\Z')


def _http_int(value: Any) -> Any:
    """Match pydantic-core 2.46.5 str_as_int/clean_int_str on HTTP strings."""
    if not isinstance(value, str):
        return value
    # Preserve the direct parser's size/error behavior before its cleanup fallback.
    if _HTTP_INTEGER.fullmatch(value):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError('Invalid integer parameter') from exc
    number = value.strip(_HTTP_WHITESPACE)
    if number.startswith('+'):
        number = number[1:]
        if number.startswith('-'):
            raise ValueError('Invalid integer parameter')
    negative = number.startswith('-')
    if negative:
        number = number[1:]
        if number.startswith(('-', '+')):
            raise ValueError('Invalid integer parameter')
    if not number or number[0] not in '0123456789-':
        raise ValueError('Invalid integer parameter')
    if number[0] == '0':
        for offset in range(1, len(number)):
            char = number[offset]
            if char in '0_':
                continue
            if char in '123456789-':
                number = number[offset:]
                break
            if char == '.':
                number = number[offset - 1 :]
                break
            raise ValueError('Invalid integer parameter')
        else:
            number = number[-1:]
    if '.' in number:
        whole, decimal = number.split('.', 1)
        if decimal and all(char == '0' for char in decimal):
            number = whole
    if (
        '_' in number
        and not number.startswith('_')
        and not number.endswith('_')
        and '__' not in number
    ):
        number = number.replace('_', '')
    if negative:
        number = '-' + number
    if not _HTTP_INTEGER.fullmatch(number):
        raise ValueError('Invalid integer parameter')
    try:
        return int(number)
    except ValueError as exc:
        raise ValueError('Invalid integer parameter') from exc


def _http_normalizer(annotation: Any) -> Callable[[Any], Any] | None:
    """Compile supported transport coercions once at route registration."""
    annotation = base_type(annotation)
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType) and type(None) in args and len(args) == 2:
        return _http_normalizer(next(arg for arg in args if arg is not type(None)))
    if origin in (list, set, frozenset, tuple):
        if origin is tuple and args and args[-1] is not Ellipsis:
            children = tuple(_http_normalizer(arg) for arg in args)
            if not any(children):
                return None

            def normalize_tuple(value: Any) -> Any:
                return (
                    [
                        cast(Callable[[Any], Any], children[i])(item)
                        if i < len(children) and children[i]
                        else item
                        for i, item in enumerate(value)
                    ]
                    if isinstance(value, (list, tuple))
                    else value
                )

            return normalize_tuple
        child = _http_normalizer(args[0]) if args else None
        if child is None:
            return None

        def normalize_sequence(value: Any) -> Any:
            return [child(item) for item in value] if isinstance(value, (list, tuple)) else value

        return normalize_sequence
    if annotation is int:
        return _http_int
    if annotation is bool:

        def normalize_bool(value: Any) -> Any:
            if isinstance(value, str):
                lowered = value.lower()
                if lowered in ('yes', 'on', 'y', 't'):
                    return True
                if lowered in ('no', 'off', 'n', 'f'):
                    return False
            return value

        return normalize_bool
    if annotation is bytes:
        return lambda value: value.encode('utf-8') if isinstance(value, str) else value
    return None


def _parameter_validator(
    target: Any, strict: bool
) -> Callable[..., tuple[Any, list[dict[str, Any]]]]:
    normalizer = None if strict else _http_normalizer(target)
    convert = msgspec.convert
    check_patterns = pattern_validator(target)

    def validate(
        value: Any, values: dict[str, Any] | None = None, *, loc: tuple[str | int, ...] = ()
    ) -> tuple[Any, list[dict[str, Any]]]:
        try:
            if normalizer is not None:
                value = normalizer(value)
            converted = convert(value, type=target, strict=strict)
            errors = check_patterns(value, loc) if check_patterns is not None else []
            return (None, errors) if errors else (converted, [])
        except (ValueError, UnicodeError, OverflowError) as exc:
            return None, [{'loc': loc, 'msg': str(exc), 'type': 'value_error.msgspec'}]

    return validate


def prepare_parameters(dependant: Dependant) -> None:
    """Keep FastAPI extraction/aliases/defaults; replace supported validators."""
    from .defaults import copy_field_metadata, guard_field_default

    for group in ('path_params', 'query_params', 'header_params', 'cookie_params'):
        for field in getattr(dependant, group):
            if getattr(field, '_msgspec_parameter', False):
                continue
            guard_field_default(field)
            copy_field_metadata(field.field_info)
            target, strict = constrained_type(
                field.field_info.annotation, field.field_info.metadata
            )
            strict = bool(strict)
            ensure_native_type(target)
            msgspec.json.schema(target)

            field.validate = _parameter_validator(target, strict)
            field._msgspec_parameter = True
            field._msgspec_type = target
    for dependency in dependant.dependencies:
        prepare_parameters(dependency)
