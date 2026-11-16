from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, MutableMapping
from functools import partial, wraps
from typing import Annotated, Any, NamedTuple, cast, get_args, get_origin, get_type_hints
from weakref import WeakValueDictionary

import anyio
import msgspec
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.datastructures import DefaultPlaceholder, _Unset
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_stream_item_type
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.utils import validation_error_definition, validation_error_response_definition
from fastapi.params import Body, Depends, Form, Param
from fastapi.routing import (
    APIRoute,
    _effective_route_context_var,
    get_request_handler,
    iter_route_contexts,
)
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.utils import deep_dict_update
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import compile_path

from .body_errors import body_errors, compile_json_body_errors
from .defaults import check_body_default, check_field_metadata, copy_body_default, copy_metadata
from .responses import _JSON_ENCODER, MsgspecJSONResponse, _encode_hook
from .validation import (
    _assert_unbranded,
    base_type,
    constrained_type,
    ensure_native_type,
    pattern_validator,
    patterns_need_raw,
    prepare_parameters,
    structured_type,
)


class _BodyConfig(NamedTuple):
    decoder: msgspec.json.Decoder[Any]
    default: Any
    alias: str | None
    check_patterns: Callable[..., list[dict[str, Any]]] | None
    pattern_needs_raw: bool
    diagnose: Callable[..., list[dict[str, Any]]] | None


_BODY_UNDEFINED = inspect.signature(Body).parameters['default'].default


def _contains_struct(annotation: Any) -> bool:
    value = base_type(annotation)
    return (isinstance(value, type) and issubclass(value, msgspec.Struct)) or any(
        _contains_struct(arg) for arg in get_args(value)
    )


def _body_type(annotation: Any) -> bool:
    value = base_type(annotation)
    if structured_type(value):
        return True
    if get_origin(value) in (list, dict, tuple, set, frozenset):
        return True
    return any(_body_type(arg) for arg in get_args(value))


def _metadata(parameter: inspect.Parameter, annotation: Any) -> list[Any]:
    items = list(get_args(annotation)[1:]) if get_origin(annotation) is Annotated else []
    return [*items, parameter.default]


def _merge_components(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for name, schema in incoming.items():
        if name in target:
            if target[name] != schema:
                raise ValueError(f'Conflicting OpenAPI component: {name}')
            continue
        target[name] = copy_metadata(schema)


def _json_type(annotation: Any) -> bool:
    if annotation in (inspect.Signature.empty, None):
        return False
    value = base_type(annotation)
    if isinstance(value, type) and issubclass(value, Response):
        return False
    return True


def _hints(endpoint: Callable[..., Any]) -> dict[str, Any]:
    target: Any = endpoint
    while isinstance(target, partial):
        target = target.func
    if not inspect.isfunction(target) and not inspect.ismethod(target):
        target = target.__call__
    return get_type_hints(target, include_extras=True)


def _parse_body(
    request: Request,
    raw: bytes,
    config: _BodyConfig,
    strict_content_type: bool,
) -> Any:
    decoder, default, body_alias, check_patterns, pattern_needs_raw, diagnose = config
    if not raw:
        if default is inspect.Signature.empty:
            # Raised after dependencies run, as in FastAPI.
            return RequestValidationError(
                [{'loc': ('body',), 'msg': 'Field required', 'type': 'missing'}]
            )
        return copy_body_default(default)
    content_type = request.headers.get('content-type')
    if content_type:
        media_type = content_type.split(';')[0].strip().lower()
        if media_type != 'application/json' and not (
            media_type.startswith('application/') and media_type.endswith('+json')
        ):
            raise HTTPException(415, 'Expected a JSON request body')
    elif strict_content_type:
        # Like FastAPI's strict_content_type: a body without a content type can be sent
        # cross-origin without a CORS preflight, so it is not parsed as JSON.
        raise HTTPException(415, 'Expected a JSON request body')
    try:
        value = decoder.decode(raw)
    except msgspec.ValidationError as exc:
        # Typed decoding can stop on a field error before discovering invalid syntax.
        try:
            if diagnose is not None:
                errors = diagnose(raw)
            else:
                errors = body_errors(msgspec.json.decode(raw), decoder.type)
        except (msgspec.DecodeError, UnicodeError) as syntax:
            raise HTTPException(400, f'Malformed JSON body: {syntax}') from syntax
        except RecursionError as nested:
            raise HTTPException(400, 'JSON body is nested too deeply') from nested
        return RequestValidationError(
            errors or [{'loc': ('body',), 'msg': str(exc), 'type': 'value_error.msgspec'}]
        )
    except (msgspec.DecodeError, UnicodeError) as exc:
        raise HTTPException(400, f'Malformed JSON body: {exc}') from exc
    except RecursionError as exc:
        raise HTTPException(400, 'JSON body is nested too deeply') from exc
    if check_patterns is not None:
        if pattern_needs_raw:
            parsed = msgspec.json.decode(raw)
            if body_alias is not None and body_alias not in parsed:
                return value.value
            pattern_value = parsed if body_alias is None else parsed[body_alias]
        else:
            pattern_value = value if body_alias is None else value.value
        loc = ('body',) if body_alias is None else ('body', body_alias)
        errors = check_patterns(pattern_value, loc)
        if errors:
            return RequestValidationError(errors)
    return value.value if body_alias is not None else value


def _revalidate_without_json(result: Any, output_type: Any, error: Exception) -> Any:
    """Validate a value whose JSON round trip failed, as for NaN and infinity.

    JSON encodes non-finite floats as null, and null is not valid for a float field.
    FastAPI with Pydantic encodes them as null in a response, so validate the Python
    values instead, with object keys converted to strings. A validation error from this
    check is reported, because the one from the JSON round trip can point at a
    non-finite float instead of the actual problem. Values that cannot be encoded as
    JSON are not revalidated.
    """
    if isinstance(error, msgspec.ValidationError):
        try:
            return msgspec.convert(
                msgspec.to_builtins(result, str_keys=True), output_type, str_keys=True
            )
        except msgspec.ValidationError as exc:
            error = exc
        except TypeError:
            # to_builtins does not support every value JSON encodes, such as msgspec.Raw.
            pass
    raise ResponseValidationError(
        [{'loc': ('response',), 'msg': str(error), 'type': 'value_error.msgspec'}]
    ) from error


async def _body_error_response(request: Request, exc: RequestValidationError) -> Response:
    return MsgspecJSONResponse({'detail': exc.errors()}, status_code=422)


class _BodyErrorHandlers(MutableMapping[Any, Any]):
    def __init__(self, handlers: MutableMapping[Any, Any]) -> None:
        self.handlers = handlers

    def __getitem__(self, key: Any) -> Any:
        handler = self.handlers[key]
        if key is RequestValidationError and handler is request_validation_exception_handler:
            return _body_error_response
        return handler

    def __setitem__(self, key: Any, value: Any) -> None:
        self.handlers[key] = value

    def __delitem__(self, key: Any) -> None:
        del self.handlers[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.handlers)

    def __len__(self) -> int:
        return len(self.handlers)

    def copy(self) -> dict[Any, Any]:
        return dict(self)


def _check_stream_event(item: Any) -> None:
    cls = type(item)
    if cls is ServerSentEvent:
        if item.raw_data is None and item.data is not None:
            raise TypeError('Use ServerSentEvent(raw_data=...) for event data')
    elif cls is not int and cls is not str and type.__subclasscheck__(ServerSentEvent, cls):
        raise TypeError('ServerSentEvent subclasses are not supported')


class _StreamItemField:
    def __init__(self, validate: Callable[..., Any], serialize_json: Callable[..., bytes]) -> None:
        self.validate = validate
        self.serialize_json = serialize_json


class MsgspecRoute(APIRoute):
    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        original = scope.get('starlette.exception_handlers')
        if (
            original is None
            or original[0].get(RequestValidationError) is not request_validation_exception_handler
        ):
            await super().handle(scope, receive, send)
            return
        # Keep native exception handling outside dependency cleanup stacks.
        installed = (_BodyErrorHandlers(original[0]), original[1])
        scope['starlette.exception_handlers'] = installed
        try:
            await super().handle(scope, receive, send)
        finally:
            if scope.get('starlette.exception_handlers') is installed:
                scope['starlette.exception_handlers'] = original

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        context = _effective_route_context_var.get()
        route = context if context is not None and context.original_route is self else self
        body_parameter = getattr(self.endpoint, '_msgspec_body_parameter', None)
        if body_parameter in compile_path(route.path)[2]:
            raise AssertionError('Path parameters must be scalar values')
        if getattr(self.endpoint, '_msgspec_stream_mode', None) == 'jsonl':
            route.is_json_stream = True
        stream_callbacks = getattr(self.endpoint, '_msgspec_stream_callbacks', None)

        def build_native_handler() -> Callable[[Request], Coroutine[Any, Any, Response]]:
            if stream_callbacks is None:
                return super(MsgspecRoute, self).get_route_handler()
            return get_request_handler(
                dependant=route.dependant,
                body_field=route.body_field,
                status_code=route.status_code,
                response_class=route.response_class,
                response_field=route.response_field,
                response_model_include=route.response_model_include,
                response_model_exclude=route.response_model_exclude,
                response_model_by_alias=route.response_model_by_alias,
                response_model_exclude_unset=route.response_model_exclude_unset,
                response_model_exclude_defaults=route.response_model_exclude_defaults,
                response_model_exclude_none=route.response_model_exclude_none,
                dependency_overrides_provider=route.dependency_overrides_provider,
                embed_body_fields=route._embed_body_fields,
                strict_content_type=route.strict_content_type,
                stream_item_field=cast(Any, _StreamItemField(*stream_callbacks)),
                is_json_stream=route.is_json_stream,
            )

        prepare_parameters(route.dependant)
        config = getattr(self.endpoint, '_msgspec_body_config', None)
        if config is None:
            pending = [route.dependant]
            while pending:
                dependant = pending.pop()
                if any(not isinstance(field.field_info, Form) for field in dependant.body_params):
                    raise TypeError('JSON body fields must use the route msgspec decoder')
                pending.extend(dependant.dependencies)
            return build_native_handler()
        stream_body = getattr(self.endpoint, '_msgspec_stream_body', None)
        if stream_body is None:
            if route.body_field is not None:
                raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')
        else:
            name, decoder, check_patterns, diagnose_stream = stream_body
            pending = [route.dependant]
            fields: list[Any] = []
            while pending:
                dependant = pending.pop()
                fields.extend(dependant.body_params)
                pending.extend(dependant.dependencies)
            if len(fields) != 1 or fields[0].name != name:
                raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')

            def validate_stream_body(
                value: Any, values: Any = None, *, loc: tuple[str | int, ...] = ()
            ) -> tuple[Any, list[dict[str, Any]]]:
                try:
                    encoded = msgspec.json.encode(value)
                    converted = decoder.decode(encoded)
                except msgspec.ValidationError as exc:
                    errors = diagnose_stream(encoded, loc)
                    return None, errors or [
                        {'loc': loc, 'msg': str(exc), 'type': 'value_error.msgspec'}
                    ]
                errors = check_patterns(value, loc) if check_patterns is not None else []
                return (None, errors) if errors else (converted, [])

            native_field = fields[0]
            if native_field.field_info.default is not _BODY_UNDEFINED:
                check_body_default(native_field.field_info.default)

                def get_stream_default() -> Any:
                    return copy_body_default(native_field.field_info.default)

                setattr(native_field, 'get_default', get_stream_default)
            setattr(native_field, 'validate', validate_stream_body)
            # Only media/syntax checks precede dependencies; native body validation runs after them.
            config = _BodyConfig(msgspec.json.Decoder(), None, None, None, True, None)

        strict_content_type = route.strict_content_type
        if isinstance(strict_content_type, DefaultPlaceholder):
            strict_content_type = strict_content_type.value

        handler = build_native_handler()

        async def handle(request: Request) -> Response:
            # A body that is not JSON is rejected before dependency side effects.
            raw = await request.body()
            request.scope['_msgspec_body_value'] = _parse_body(
                request, raw, config, strict_content_type
            )
            response = await handler(request)
            return response

        return handle


def _rename_schema_refs(schema: dict[str, Any], names: dict[str, str]) -> None:
    if '$ref' in schema:
        schema['$ref'] = names.get(schema['$ref'], schema['$ref'])
    mapping = schema.get('discriminator', {}).get('mapping', {})
    for key, ref in mapping.items():
        mapping[key] = names.get(ref, ref)
    # Visit schema positions only; defaults, examples and property names are data.
    for key in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'):
        for child in schema.get(key, {}).values():
            if isinstance(child, dict):
                _rename_schema_refs(child, names)
    for key in (
        'items',
        'additionalProperties',
        'unevaluatedProperties',
        'propertyNames',
        'contains',
        'not',
        'if',
        'then',
        'else',
        'unevaluatedItems',
        'contentSchema',
    ):
        child = schema.get(key)
        if isinstance(child, dict):
            _rename_schema_refs(child, names)
    for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
        for child in schema.get(key, []):
            if isinstance(child, dict):
                _rename_schema_refs(child, names)


def install_openapi(app: FastAPI, *routers: 'MsgspecRouter') -> None:
    """Install once before serving; router arguments are kept for compatibility."""
    if getattr(app, '_msgspec_openapi_installed', False):
        raise RuntimeError('msgspec OpenAPI is already installed')
    # FastAPI builds included contexts lazily; validate them before serving.
    list(iter_route_contexts(app.routes))
    list(iter_route_contexts(app.webhooks.routes))
    original = app.openapi
    app.openapi_schema = None

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = original()
        # Do not retain a partially patched document if schema generation fails.
        app.openapi_schema = None
        components = document.setdefault('components', {}).setdefault('schemas', {})
        targets: list[tuple[Any, dict[str, Any]]] = []
        body_extras: dict[int, tuple[Callable[..., Any], dict[str, Any]]] = {}

        def collect_fields(dependant: Dependant) -> list[Any]:
            fields = []
            for group in ('path_params', 'query_params', 'header_params', 'cookie_params'):
                fields.extend(getattr(dependant, group))
            for dependency in dependant.dependencies:
                fields.extend(collect_fields(dependency))
            return fields

        def collect(routes: Any, paths: dict[str, Any]) -> None:
            operations = {}
            for route in iter_route_contexts(routes):
                if isinstance(route.original_route, APIRoute) and route.include_in_schema:
                    path = route.path_format
                    if path is None:
                        continue
                    for method in route.methods or ():
                        operations[path, method.lower()] = route
            for (path, method), route in operations.items():
                operation = paths.get(path, {}).get(method)
                if operation is None:
                    continue
                body_type, output_type, output_code = getattr(
                    route.endpoint, '_msgspec_openapi', (None, None, None)
                )
                if body_type is not None:
                    body_schema = operation['requestBody']['content']['application/json']['schema']
                    alias = getattr(route.endpoint, '_msgspec_body_alias', None)
                    if alias is not None:
                        body_schema = body_schema['properties'][alias]
                    extra = getattr(route.endpoint, '_msgspec_body_schema_extra', None)
                    if extra is not None:
                        body_extras[id(body_schema)] = extra
                    targets.append(
                        (
                            body_type,
                            body_schema,
                        )
                    )
                    _merge_components(
                        components,
                        {
                            'HTTPValidationError': validation_error_response_definition,
                            'ValidationError': validation_error_definition,
                        },
                    )
                if (
                    output_type is not None
                    and getattr(route.endpoint, '_msgspec_response_media_type', None) is not None
                ):
                    stream_mode = getattr(route.endpoint, '_msgspec_stream_mode', None)
                    media_type = getattr(route.endpoint, '_msgspec_response_media_type', None)
                    content = operation['responses'][str(output_code)]['content'][media_type]
                    schema = content['itemSchema' if stream_mode else 'schema']
                    if stream_mode == 'sse':
                        required = schema.setdefault('required', [])
                        if 'data' not in required:
                            required.append('data')
                        data = schema.setdefault('properties', {}).setdefault(
                            'data', {'type': 'string'}
                        )
                        data['contentMediaType'] = 'application/json'
                        schema = data.setdefault('contentSchema', {})
                    targets.append((output_type, schema))
                fields = collect_fields(route.dependant)
                for parameter in operation.get('parameters', []):
                    for field in fields:
                        if (
                            getattr(field, '_msgspec_parameter', False)
                            and parameter['in'] == cast(Param, field.field_info).in_.value
                            and parameter['name'] == field.alias
                        ):
                            # Replace the Pydantic schema, preserving field metadata.
                            previous = parameter.get('schema', {})
                            schema = {
                                key: previous[key]
                                for key in ('title', 'description', 'default')
                                if key in previous
                            }
                            parameter['schema'] = schema
                            targets.append((getattr(field, '_msgspec_type'), schema))
                callbacks = {
                    callback.name: callback
                    for callback in route.callbacks or ()
                    if isinstance(callback, APIRoute)
                }
                for callback in callbacks.values():
                    callback_paths = operation.get('callbacks', {}).get(callback.name, {})
                    collect([callback], callback_paths)

        collect(app.routes, document.get('paths', {}))
        collect(app.webhooks.routes, document.get('webhooks', {}))
        if targets:
            schemas, definitions = msgspec.json.schema_components(
                [annotation for annotation, _ in targets],
                ref_template='#/components/schemas/{name}',
            )
            # Pydantic and msgspec can use the same short name for different schemas.
            reserved = set(components) | set(definitions)
            names = {}
            for name in definitions:
                if name in components:
                    candidate = name + '_Msgspec'
                    while candidate in reserved:
                        candidate += '_'
                    reserved.add(candidate)
                    names['#/components/schemas/' + name] = '#/components/schemas/' + candidate
            for schema in (*schemas, *definitions.values()):
                _rename_schema_refs(schema, names)
            definitions = {
                names.get('#/components/schemas/' + name, '#/components/schemas/' + name).rsplit(
                    '/', 1
                )[-1]: schema
                for name, schema in definitions.items()
            }
            _merge_components(components, definitions)
            for (_, target), schema in zip(targets, schemas):
                schema = copy_metadata(schema)
                if extra := body_extras.get(id(target)):
                    callback, metadata = extra
                    schema.update(metadata)
                    callback(schema)
                deep_dict_update(schema, target)
                target.clear()
                target.update(schema)
        app.openapi_schema = document
        return document

    setattr(app, 'openapi', openapi)
    setattr(app, '_msgspec_openapi_installed', True)


class MsgspecRouter(APIRouter):
    """Typed msgspec JSON bodies and validated responses for ordinary CRUD routes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault('route_class', MsgspecRoute)
        kwargs.setdefault('default_response_class', MsgspecJSONResponse)
        if not issubclass(kwargs['route_class'], MsgspecRoute):
            raise TypeError('route_class must extend MsgspecRoute')
        self._body_error_checks: WeakValueDictionary[Any, Callable[..., list[dict[str, Any]]]] = (
            WeakValueDictionary()
        )
        super().__init__(*args, **kwargs)

    def _body_error_check(self, annotation: Any) -> Callable[..., list[dict[str, Any]]]:
        check = self._body_error_checks.get(annotation)
        if check is None:
            check = compile_json_body_errors(annotation)
            self._body_error_checks[annotation] = check
        return check

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        override = kwargs.get('route_class_override')
        if override is not None and not issubclass(override, MsgspecRoute):
            raise TypeError('route_class_override must extend MsgspecRoute')
        # Routes copied from another MsgspecRouter with add_api_route already have a
        # prepared signature and response metadata.
        if getattr(endpoint, '_msgspec_prepared', False):
            return super().add_api_route(path, endpoint, **kwargs)
        signature = inspect.signature(endpoint)
        hints = _hints(endpoint)
        parameters = [
            p.replace(annotation=hints.get(p.name, p.annotation))
            for p in signature.parameters.values()
        ]
        return_type = hints.get('return', signature.return_annotation)
        if return_type is type(None):
            # As in FastAPI, `-> None` declares no response model.
            return_type = None
        response_class = kwargs.get('response_class')
        if response_class is None or isinstance(response_class, DefaultPlaceholder):
            response_class = self.default_response_class
        if isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value
        target_endpoint = (
            endpoint
            if inspect.isfunction(endpoint)
            or inspect.ismethod(endpoint)
            or isinstance(endpoint, partial)
            else getattr(endpoint, '__call__', endpoint)
        )
        is_async_generator = inspect.isasyncgenfunction(target_endpoint)
        is_generator = is_async_generator or inspect.isgeneratorfunction(target_endpoint)
        stream_mode = None
        stream_item = get_stream_item_type(return_type) if is_generator else None
        if is_generator:
            explicit_class = kwargs.get('response_class')
            if issubclass(response_class, EventSourceResponse):
                stream_mode = 'sse'
            elif (
                explicit_class is None or isinstance(explicit_class, DefaultPlaceholder)
            ) and response_class is MsgspecJSONResponse:
                stream_mode = 'jsonl'
            else:
                stream_mode = 'raw'
        explicit = kwargs.get('response_model', inspect.Signature.empty)
        inferred = explicit is inspect.Signature.empty or isinstance(explicit, DefaultPlaceholder)
        output_type = return_type if inferred else explicit
        if is_generator and stream_mode != 'raw' and inferred:
            output_type = stream_item if stream_item is not None else inspect.Signature.empty
        if (
            is_generator
            and stream_mode == 'sse'
            and isinstance(stream_item, type)
            and type.__subclasscheck__(ServerSentEvent, stream_item)
        ):
            if stream_item is not ServerSentEvent:
                raise TypeError('ServerSentEvent subclasses are not supported')
            if inferred:
                output_type = inspect.Signature.empty
        validates_output = stream_mode != 'raw' and _json_type(output_type)
        if validates_output:
            output_type, output_strict = constrained_type(output_type)
            ensure_native_type(output_type)
            msgspec.json.schema(output_type)
            if output_strict is False:
                raise TypeError('JSON msgspec responses use strict validation')
        output_decoder = msgspec.json.Decoder(type=output_type) if validates_output else None
        output_patterns = pattern_validator(output_type) if validates_output else None
        # Like FastAPI with a Pydantic response_model at the top level: an instance of exactly
        # the declared Struct class is trusted and encoded once; other top-level values,
        # including subclass instances, are validated against the schema. Unlike Pydantic,
        # values nested in a trusted instance are encoded as they are (a nested subclass keeps
        # its extra fields, and the type parameters of a generic Struct are not checked).
        output_class = (
            (get_origin(base_type(output_type)) or base_type(output_type))
            if validates_output
            else None
        )
        trusts_instances = isinstance(output_class, type) and issubclass(
            output_class, msgspec.Struct
        )
        output_encoder = msgspec.json.Encoder() if validates_output else None
        serializes_untyped_output = output_type in (inspect.Signature.empty, None) and (
            response_class is MsgspecJSONResponse or stream_mode == 'sse'
        )
        handles_output = stream_mode != 'raw'
        untyped_encoder = (
            msgspec.json.Encoder(enc_hook=_encode_hook) if serializes_untyped_output else None
        )
        path_parameters = compile_path(path)[2]
        body = None
        body_meta = None
        for parameter in parameters:
            metadata = _metadata(parameter, parameter.annotation)
            if any(isinstance(x, Depends) for x in metadata):
                continue
            # Form and File parameters are left to FastAPI.
            if any(isinstance(x, Form) for x in metadata):
                if _contains_struct(parameter.annotation):
                    raise TypeError('msgspec Structs are not supported as Form or File parameters')
                continue
            pending_types = [parameter.annotation]
            while pending_types:
                current = base_type(pending_types.pop())
                if isinstance(current, type):
                    _assert_unbranded(current)
                elif get_origin(current) is not None:
                    pending_types.extend(get_args(current))
            is_body = _contains_struct(parameter.annotation) or any(
                isinstance(x, Body) for x in metadata
            )
            if _body_type(parameter.annotation) and not any(isinstance(x, Param) for x in metadata):
                is_body = True
            if not is_body:
                for item in metadata:
                    if isinstance(item, Param):
                        check_field_metadata(item)
                parameter_default = parameter.default
                if isinstance(parameter_default, Param):
                    parameter_default = parameter_default.default
                if (
                    parameter_default is not inspect.Signature.empty
                    and parameter_default is not _BODY_UNDEFINED
                    and parameter_default is not Ellipsis
                ):
                    check_body_default(parameter_default)
            if is_body and _json_type(parameter.annotation):
                if parameter.name in path_parameters:
                    raise AssertionError('Path parameters must be scalar values')
                if any(isinstance(x, Param) for x in metadata):
                    raise TypeError(
                        'msgspec Struct query/path/header/cookie parameters are not supported'
                    )
                if body is not None:
                    raise TypeError(
                        'Only one msgspec body parameter is supported; other body fields cannot be combined'
                    )
                body = parameter
                body_meta = next((x for x in metadata if isinstance(x, Body)), None)

        if body is None and not handles_output and (not is_generator or stream_mode == 'raw'):
            return super().add_api_route(path, endpoint, **kwargs)
        if any(p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL) for p in parameters):
            raise TypeError('Variadic msgspec endpoints are not supported')
        if handles_output:
            if validates_output:
                for option in (
                    'response_model_include',
                    'response_model_exclude',
                    'response_model_exclude_unset',
                    'response_model_exclude_defaults',
                    'response_model_exclude_none',
                ):
                    if kwargs.get(option):
                        raise TypeError(f'{option} is not supported for msgspec responses')
                if kwargs.get('response_model_by_alias', True) is False:
                    raise TypeError('msgspec responses use encoded field names')
            if validates_output and kwargs.get('status_code') in (204, 304):
                raise TypeError('A typed msgspec response requires a response body')
            kwargs['response_model'] = None
            if not is_generator:
                kwargs['response_class'] = response_class

        responses = copy_metadata(kwargs.get('responses') or {})
        openapi_extra = copy_metadata(kwargs.get('openapi_extra') or {})
        default = inspect.Signature.empty
        decoder = None
        embedded = False
        body_alias = None
        schema_body_type = None
        if body is not None:
            metadata = (
                [x for x in get_args(body.annotation)[1:] if not isinstance(x, Body)]
                if get_origin(body.annotation) is Annotated
                else []
            )
            body_type, body_strict = constrained_type(base_type(body.annotation), metadata)
            if body_strict is False:
                raise TypeError('JSON msgspec bodies use strict validation')
            if body_meta is not None:
                if body_meta.media_type != 'application/json':
                    raise TypeError('Only application/json msgspec bodies are supported')
                body_type, body_strict = constrained_type(body_type, body_meta.metadata)
                if body_strict is False:
                    raise TypeError('JSON msgspec bodies use strict validation')
                embedded = body_meta.embed is True
                default = body_meta.default
                if default is _BODY_UNDEFINED or default is Ellipsis:
                    default = inspect.Signature.empty
                if (
                    not isinstance(body.default, Body)
                    and body.default is not inspect.Signature.empty
                ):
                    default = body.default
                if body_meta.default_factory is not None:
                    raise TypeError('Body default_factory is not supported')
            else:
                default = body.default
            if default is not inspect.Signature.empty:
                check_body_default(default)
            ensure_native_type(body_type)
            msgspec.json.schema(body_type)
            field_schema: dict[str, Any] = {}
            if embedded and default is not inspect.Signature.empty:
                field_schema['default'] = msgspec.to_builtins(default, str_keys=True)
            if body_meta is not None:
                for key in ('title', 'description', 'examples', 'deprecated'):
                    value = getattr(body_meta, key)
                    if value is not None:
                        field_schema[key] = value
                if isinstance(body_meta.json_schema_extra, dict):
                    field_schema.update(body_meta.json_schema_extra)
                elif body_meta.json_schema_extra is not None and not callable(
                    body_meta.json_schema_extra
                ):
                    raise TypeError('Body json_schema_extra must be a dict or callable')
            if field_schema:
                field_schema = copy_metadata(field_schema)
            decode_type = body_type
            if embedded:
                assert body_meta is not None
                # As in FastAPI, the key read from the request is a string validation_alias,
                # falling back to alias; AliasChoices and AliasPath do not change it.
                validation_alias = body_meta.validation_alias
                if not isinstance(validation_alias, str):
                    validation_alias = None
                body_alias = validation_alias or body_meta.alias or body.name
                # One explicit JSON envelope, preserving a possible Body alias.
                field = (
                    ('value', body_type)
                    if default is inspect.Signature.empty
                    else (
                        'value',
                        body_type,
                        msgspec.field(default_factory=lambda: copy_body_default(default)),
                    )
                )
                decode_type = msgspec.defstruct(
                    'MsgspecBody', [field], rename={'value': body_alias}
                )
            decoder = msgspec.json.Decoder(type=decode_type)
            schema_callback = body_meta.json_schema_extra if body_meta is not None else None
            schema_body_type = body_type
            body_schema = {} if callable(schema_callback) else field_schema
            request_schema = body_schema
            if body_alias is not None:
                request_schema = {
                    'type': 'object',
                    'properties': {body_alias: body_schema},
                }
                if default is inspect.Signature.empty:
                    request_schema['required'] = [body_alias]
            media: dict[str, Any] = {'schema': request_schema}
            if body_meta is not None and not embedded:
                if body_meta.openapi_examples:
                    media['examples'] = copy_metadata(body_meta.openapi_examples)
                elif body_meta.example is not _Unset:
                    media['example'] = copy_metadata(body_meta.example)
            request_body = {
                'required': default is inspect.Signature.empty,
                'content': {'application/json': media},
            }
            deep_dict_update(request_body, openapi_extra.get('requestBody', {}))
            openapi_extra['requestBody'] = request_body
            responses.setdefault(400, {'description': 'Malformed JSON'})
            responses.setdefault(415, {'description': 'Unsupported Media Type'})
            responses.setdefault(
                422,
                {
                    'description': 'Validation Error',
                    'content': {
                        'application/json': {
                            'schema': {'$ref': '#/components/schemas/HTTPValidationError'}
                        }
                    },
                },
            )
        output_media = (
            'application/jsonl'
            if stream_mode == 'jsonl'
            else 'text/event-stream'
            if stream_mode == 'sse'
            else response_class.media_type
        )
        if validates_output and output_media is not None:
            code = kwargs.get('status_code') or 200
            response_key = str(code) if str(code) in responses else code
            result_meta = responses.setdefault(response_key, {})
            result_meta.setdefault(
                'description', kwargs.get('response_description', 'Successful Response')
            )
            media_type = output_media
            result_meta.setdefault('content', {}).setdefault(media_type, {}).setdefault(
                'itemSchema' if is_generator else 'schema', {}
            )
        kwargs['responses'] = responses
        kwargs['openapi_extra'] = openapi_extra

        remaining = [p for p in parameters if p is not body]
        if is_generator and body is not None:
            extraction = (
                Body(
                    default=body_meta.default,
                    alias=body_meta.alias,
                    validation_alias=body_meta.validation_alias,
                    serialization_alias=body_meta.serialization_alias,
                    alias_priority=body_meta.alias_priority,
                    embed=body_meta.embed,
                    media_type=body_meta.media_type,
                    include_in_schema=body_meta.include_in_schema,
                )
                if body_meta is not None
                else Body()
            )
            replacement = body.replace(annotation=Annotated[Any, extraction])
            remaining = [
                replacement if parameter is body else parameter for parameter in parameters
            ]

        def injected_name(type_: type[Request] | type[Response], fallback: str) -> tuple[str, bool]:
            for p in remaining:
                annotation = base_type(p.annotation)
                if isinstance(annotation, type) and issubclass(annotation, type_):
                    return p.name, False
            names = {p.name for p in parameters}
            while fallback in names:
                fallback += '_'
            remaining.append(
                inspect.Parameter(fallback, kind=inspect.Parameter.KEYWORD_ONLY, annotation=type_)
            )
            return fallback, True

        request_name, remove_request = (
            injected_name(Request, '__msgspec_request')
            if body and not is_generator
            else (None, False)
        )
        response_name, remove_response = (
            injected_name(Response, '__msgspec_response')
            if handles_output and not is_generator
            else (None, False)
        )

        is_async = inspect.iscoroutinefunction(endpoint) or inspect.iscoroutinefunction(
            getattr(endpoint, '__call__', None)
        )

        def validate_result(result: Any) -> Any:
            if validates_output and not (trusts_instances and type(result) is output_class):
                try:
                    result = cast(msgspec.json.Decoder[Any], output_decoder).decode(
                        cast(msgspec.json.Encoder, output_encoder).encode(result)
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    result = _revalidate_without_json(result, output_type, exc)
            if output_patterns is not None:
                errors = output_patterns(result, ('response',))
                if errors:
                    raise ResponseValidationError(errors)
            return result

        @wraps(endpoint)
        async def wrapper(**values: Any) -> Any:
            sub_response = cast(Response, values.get(cast(str, response_name)))
            if remove_response:
                values.pop(cast(str, response_name))
            if body is not None:
                request = values[cast(str, request_name)]
                if remove_request:
                    values.pop(cast(str, request_name))
                value = request.scope['_msgspec_body_value']
                if isinstance(value, RequestValidationError):
                    raise value
                values[body.name] = value
            if is_async:
                result = await endpoint(**values)
            else:
                result = await run_in_threadpool(endpoint, **values)
                if inspect.isawaitable(result):
                    result = await result
            if isinstance(result, Response):
                return result
            result = validate_result(result)
            status_code = sub_response.status_code or kwargs.get('status_code') or 200
            if untyped_encoder is not None:
                response = Response(
                    untyped_encoder.encode(result),
                    status_code=status_code,
                    media_type='application/json',
                )
            elif issubclass(response_class, MsgspecJSONResponse):
                response = response_class(result, status_code=status_code)
            elif issubclass(response_class, JSONResponse):
                response = response_class(
                    msgspec.json.decode(_JSON_ENCODER.encode(result)),
                    status_code=status_code,
                )
            else:
                response = response_class(
                    msgspec.to_builtins(result, str_keys=True), status_code=status_code
                )
            if response.status_code < 200 or response.status_code in (204, 205, 304):
                response.body = b''
                if 'content-length' in response.headers:
                    del response.headers['content-length']
            response.raw_headers.extend(sub_response.raw_headers)
            return response

        stream_encoder = (
            msgspec.json.Encoder(enc_hook=_encode_hook) if is_generator and handles_output else None
        )

        def validate_stream_item(
            value: Any,
            values: dict[str, Any] | None = None,
            *,
            loc: tuple[str | int, ...] = (),
        ) -> tuple[Any, list[dict[str, Any]]]:
            return validate_result(value), []

        def serialize_stream_item(value: Any, **options: Any) -> bytes:
            encoded = cast(msgspec.json.Encoder, stream_encoder).encode(value)
            # Raw must satisfy the same JSON decoding checks as other output.
            msgspec.json.decode(encoded)
            if b'\n' in encoded or b'\r' in encoded:
                encoded = msgspec.json.format(encoded, indent=-1)
            return encoded

        @wraps(endpoint)
        async def stream_wrapper(**values: Any) -> AsyncIterator[Any]:
            generator = endpoint(**values)
            try:
                items = generator if is_async_generator else iterate_in_threadpool(generator)
                if stream_mode == 'sse':
                    async for item in items:
                        _check_stream_event(item)
                        yield item
                else:
                    async for item in items:
                        yield item
            finally:
                with anyio.CancelScope(shield=True):
                    if is_async_generator:
                        await generator.aclose()
                    else:
                        await run_in_threadpool(generator.close)

        prepared_endpoint: Callable[..., Any] = stream_wrapper if is_generator else wrapper
        setattr(
            prepared_endpoint,
            '__signature__',
            signature.replace(
                parameters=remaining,
                return_annotation=inspect.Signature.empty if handles_output else return_type,
            ),
        )
        setattr(prepared_endpoint, '_msgspec_prepared', True)
        setattr(prepared_endpoint, '_msgspec_response_media_type', output_media)
        if is_generator:
            setattr(prepared_endpoint, '_msgspec_stream_mode', stream_mode)
            if handles_output and stream_mode in ('jsonl', 'sse'):
                setattr(
                    prepared_endpoint,
                    '_msgspec_stream_callbacks',
                    (validate_stream_item, serialize_stream_item),
                )
        setattr(
            prepared_endpoint,
            '_msgspec_openapi',
            (
                schema_body_type,
                output_type if validates_output else None,
                kwargs.get('status_code') or 200,
            ),
        )
        if body is not None:
            assert decoder is not None
            pattern_needs_raw = patterns_need_raw(body_type) or (
                body_alias is not None and default is not inspect.Signature.empty
            )
            setattr(
                prepared_endpoint,
                '_msgspec_body_config',
                _BodyConfig(
                    decoder,
                    default,
                    body_alias,
                    pattern_validator(body_type, strings_validated=not pattern_needs_raw),
                    pattern_needs_raw,
                    self._body_error_check(decoder.type),
                ),
            )
            setattr(prepared_endpoint, '_msgspec_body_parameter', body.name)
            if is_generator:
                setattr(
                    prepared_endpoint,
                    '_msgspec_stream_body',
                    (
                        body.name,
                        msgspec.json.Decoder(type=body_type),
                        pattern_validator(body_type),
                        self._body_error_check(body_type),
                    ),
                )
            if body_alias is not None:
                setattr(prepared_endpoint, '_msgspec_body_alias', body_alias)
            if callable(schema_callback):
                setattr(
                    prepared_endpoint,
                    '_msgspec_body_schema_extra',
                    (schema_callback, copy_metadata(field_schema)),
                )
        route_count = len(self.routes)
        try:
            result = super().add_api_route(path, prepared_endpoint, **kwargs)
            if (
                body is not None
                and not is_generator
                and cast(APIRoute, self.routes[-1]).body_field is not None
            ):
                raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')
            return result
        except Exception:
            del self.routes[route_count:]
            raise
