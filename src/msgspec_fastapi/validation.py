"""Route-local validation for HTTP parameters; no global FastAPI changes."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import is_dataclass
from types import UnionType
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin, get_type_hints

import annotated_types
import msgspec
from fastapi.dependencies.models import Dependant
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from typing_extensions import is_typeddict


def base_type(annotation: Any) -> Any:
    return get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation


def structured_type(annotation: Any) -> bool:
    value = get_origin(base_type(annotation)) or base_type(annotation)
    return isinstance(value, type) and (
        issubclass(value, msgspec.Struct)
        or is_typeddict(value)
        or is_dataclass(value)
        or (issubclass(value, tuple) and hasattr(value, '_fields'))
    )


def uses_pydantic(annotation: Any) -> bool:
    value = base_type(annotation)
    return (isinstance(value, type) and issubclass(value, BaseModel)) or any(
        uses_pydantic(item) for item in get_args(value)
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
        if isinstance(item, FieldInfo):
            expanded.extend(item.metadata)
        else:
            expanded.append(item)
    metadata = expanded
    constraints: dict[str, Any] = {}
    metas = []
    strict = None
    for item in metadata:
        if isinstance(item, msgspec.Meta):
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
        elif type(item).__module__.startswith('pydantic') and type(item).__name__ == 'Strict':
            strict = item.strict
        elif (
            type(item).__module__.startswith('pydantic')
            and type(item).__name__ == '_PydanticGeneralMetadata'
        ):
            for name, value in vars(item).items():
                if name not in ('pattern',):
                    raise TypeError(f'Unsupported msgspec parameter constraint: {name}')
                constraints[name] = value
        else:
            raise TypeError(f'Unsupported msgspec parameter metadata: {type(item).__name__}')
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


# Unicode White_Space used by Rust str::trim, excluding Python's extra C0 separators.
_HTTP_WHITESPACE = '\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000'
_HTTP_INTEGER = re.compile(r'-?(?:0|[1-9][0-9]*)\Z')


def _http_int(value: Any) -> Any:
    """Match pydantic-core 2.46.5 str_as_int/clean_int_str on HTTP strings."""
    if not isinstance(value, str):
        return value
    # Preserve the direct parser's size/error behavior before its cleanup fallback.
    if _HTTP_INTEGER.fullmatch(value):
        return int(value)
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
    return int(number)


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

    def validate(
        value: Any, values: dict[str, Any] | None = None, *, loc: tuple[str | int, ...] = ()
    ) -> tuple[Any, list[dict[str, Any]]]:
        try:
            if normalizer is not None:
                value = normalizer(value)
            return convert(value, type=target, strict=strict), []
        except (ValueError, UnicodeError, OverflowError) as exc:
            return None, [{'loc': loc, 'msg': str(exc), 'type': 'value_error.msgspec'}]

    return validate


def prepare_parameters(dependant: Dependant) -> None:
    """Keep FastAPI extraction/aliases/defaults; replace supported validators."""
    for group in ('path_params', 'query_params', 'header_params', 'cookie_params'):
        for field in getattr(dependant, group):
            if uses_pydantic(field.field_info.annotation):
                continue
            if getattr(field, '_msgspec_parameter', False):
                continue
            target, strict = constrained_type(
                field.field_info.annotation, field.field_info.metadata
            )
            strict = bool(strict)
            schema = msgspec.json.schema(target)

            field.validate = _parameter_validator(target, strict)
            field._msgspec_parameter = True
            field._msgspec_schema = schema
    for dependency in dependant.dependencies:
        prepare_parameters(dependency)
