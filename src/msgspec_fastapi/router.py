from __future__ import annotations

import inspect
from collections.abc import Callable, Coroutine
from copy import deepcopy
from functools import partial, wraps
from typing import Annotated, Any, cast, get_args, get_origin, get_type_hints

import msgspec
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi._compat import ModelField
from fastapi.datastructures import DefaultPlaceholder, _Unset
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.utils import validation_error_definition, validation_error_response_definition
from fastapi.params import Body, Depends, Form, Param
from fastapi.routing import APIRoute, _effective_route_context_var, iter_route_contexts
from fastapi.utils import deep_dict_update
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.routing import compile_path

from .responses import MsgspecJSONResponse
from .validation import (
    base_type,
    constrained_type,
    prepare_parameters,
    structured_type,
    uses_pydantic,
)


def _untyped_encode_hook(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json', by_alias=True)
    raise TypeError(f'Encoding objects of type {type(value).__name__} is unsupported')


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
        target[name] = deepcopy(schema)


def _json_type(annotation: Any) -> bool:
    if annotation in (inspect.Signature.empty, None) or uses_pydantic(annotation):
        return False
    value = base_type(annotation)
    if isinstance(value, type) and issubclass(value, Response):
        return False
    try:
        msgspec.json.Decoder(type=annotation)
        msgspec.json.schema(annotation)
    except (TypeError, ValueError):
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
    config: tuple[msgspec.json.Decoder[Any], Any, bool],
    strict_content_type: bool,
) -> Any:
    decoder, default, embedded = config
    if not raw:
        if default is inspect.Signature.empty:
            # Raised after dependencies run, as in FastAPI.
            return RequestValidationError(
                [{'loc': ('body',), 'msg': 'Field required', 'type': 'missing'}]
            )
        return deepcopy(default)
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
            msgspec.json.decode(raw)
        except (msgspec.DecodeError, UnicodeError) as syntax:
            raise HTTPException(400, f'Malformed JSON body: {syntax}') from syntax
        except RecursionError as nested:
            raise HTTPException(400, 'JSON body is nested too deeply') from nested
        return RequestValidationError(
            [{'loc': ('body',), 'msg': str(exc), 'type': 'value_error.msgspec'}]
        )
    except (msgspec.DecodeError, UnicodeError) as exc:
        raise HTTPException(400, f'Malformed JSON body: {exc}') from exc
    except RecursionError as exc:
        raise HTTPException(400, 'JSON body is nested too deeply') from exc
    return value.value if embedded else value


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


class MsgspecRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        context = _effective_route_context_var.get()
        route = context if context is not None and context.original_route is self else self
        body_parameter = getattr(self.endpoint, '_msgspec_body_parameter', None)
        if body_parameter in compile_path(route.path)[2]:
            raise AssertionError('Path parameters must be scalar values')
        prepare_parameters(route.dependant)
        handler = super().get_route_handler()
        config = getattr(self.endpoint, '_msgspec_body_config', None)
        if config is None:
            return handler
        if route.body_field is not None:
            raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')

        strict_content_type = route.strict_content_type
        if isinstance(strict_content_type, DefaultPlaceholder):
            strict_content_type = strict_content_type.value

        async def handle(request: Request) -> Response:
            # A body that is not JSON is rejected before dependency side effects.
            request.scope['_msgspec_body_value'] = _parse_body(
                request, await request.body(), config, strict_content_type
            )
            return await handler(request)

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

        def collect_fields(dependant: Dependant) -> list[ModelField]:
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
                if output_type is not None:
                    targets.append(
                        (
                            output_type,
                            operation['responses'][str(output_code)]['content']['application/json'][
                                'schema'
                            ],
                        )
                    )
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
                schema = deepcopy(schema)
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
        super().__init__(*args, **kwargs)

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
        explicit = kwargs.get('response_model', inspect.Signature.empty)
        inferred = explicit is inspect.Signature.empty or isinstance(explicit, DefaultPlaceholder)
        output_type = return_type if inferred else explicit
        validates_output = _json_type(output_type)
        if validates_output:
            output_type, output_strict = constrained_type(output_type)
            if output_strict is False:
                raise TypeError('JSON msgspec responses use strict validation')
        output_decoder = msgspec.json.Decoder(type=output_type) if validates_output else None
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
        response_class = kwargs.get('response_class')
        if response_class is None or isinstance(response_class, DefaultPlaceholder):
            response_class = self.default_response_class
        if isinstance(response_class, DefaultPlaceholder):
            response_class = response_class.value
        serializes_untyped_output = (
            output_type in (inspect.Signature.empty, None)
            and not uses_pydantic(return_type)
            and response_class is MsgspecJSONResponse
        )
        handles_output = (
            validates_output
            or serializes_untyped_output
            or (explicit is None and _contains_struct(return_type))
        )
        untyped_encoder = (
            msgspec.json.Encoder(enc_hook=_untyped_encode_hook)
            if serializes_untyped_output
            else None
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
            is_body = _contains_struct(parameter.annotation) or any(
                isinstance(x, Body) for x in metadata
            )
            if _body_type(parameter.annotation) and not any(isinstance(x, Param) for x in metadata):
                is_body = True
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

        if body is None and not handles_output:
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
            response_class = kwargs.get('response_class')
            if (
                response_class is not None
                and not isinstance(response_class, DefaultPlaceholder)
                and response_class is not MsgspecJSONResponse
            ):
                raise TypeError(
                    'Custom response_class is not supported for typed msgspec responses'
                )
            kwargs['response_model'] = None
            kwargs['response_class'] = MsgspecJSONResponse

        responses = deepcopy(kwargs.get('responses') or {})
        openapi_extra = deepcopy(kwargs.get('openapi_extra') or {})
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
                from pydantic_core import PydanticUndefined

                if default is PydanticUndefined or default is Ellipsis:
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
            decode_type = body_type
            if embedded:
                assert body_meta is not None
                body_alias = body_meta.alias or body.name
                # One explicit JSON envelope, preserving a possible Body alias.
                field = (
                    ('value', body_type)
                    if default is inspect.Signature.empty
                    else (
                        'value',
                        body_type,
                        msgspec.field(default_factory=lambda: deepcopy(default)),
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
                    media['examples'] = body_meta.openapi_examples
                elif body_meta.example is not _Unset:
                    media['example'] = body_meta.example
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
        if validates_output:
            code = kwargs.get('status_code') or 200
            response_key = str(code) if str(code) in responses else code
            result_meta = responses.setdefault(response_key, {})
            result_meta.setdefault(
                'description', kwargs.get('response_description', 'Successful Response')
            )
            result_meta.setdefault('content', {}).setdefault('application/json', {}).setdefault(
                'schema', {}
            )
        kwargs['responses'] = responses
        kwargs['openapi_extra'] = openapi_extra

        remaining = [p for p in parameters if p is not body]

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
            injected_name(Request, '__msgspec_request') if body else (None, False)
        )
        response_name, remove_response = (
            injected_name(Response, '__msgspec_response') if handles_output else (None, False)
        )

        is_async = inspect.iscoroutinefunction(endpoint) or inspect.iscoroutinefunction(
            getattr(endpoint, '__call__', None)
        )

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
            if not handles_output:
                return msgspec.to_builtins(result) if _contains_struct(return_type) else result
            if validates_output and not (trusts_instances and type(result) is output_class):
                try:
                    # Typed decoding validates fields and Meta, even for existing Structs.
                    result = cast(msgspec.json.Decoder[Any], output_decoder).decode(
                        cast(msgspec.json.Encoder, output_encoder).encode(result)
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    result = _revalidate_without_json(result, output_type, exc)
            status_code = sub_response.status_code or kwargs.get('status_code') or 200
            if untyped_encoder is not None:
                response = Response(
                    untyped_encoder.encode(result),
                    status_code=status_code,
                    media_type='application/json',
                )
            else:
                response = MsgspecJSONResponse(result, status_code=status_code)
            if response.status_code < 200 or response.status_code in (204, 205, 304):
                response.body = b''
                if 'content-length' in response.headers:
                    del response.headers['content-length']
            response.raw_headers.extend(sub_response.raw_headers)
            return response

        setattr(
            wrapper,
            '__signature__',
            signature.replace(
                parameters=remaining,
                return_annotation=inspect.Signature.empty if handles_output else return_type,
            ),
        )
        setattr(wrapper, '_msgspec_prepared', True)
        setattr(
            wrapper,
            '_msgspec_openapi',
            (
                schema_body_type,
                output_type if validates_output else None,
                kwargs.get('status_code') or 200,
            ),
        )
        if body is not None:
            setattr(wrapper, '_msgspec_body_config', (decoder, default, embedded))
            setattr(wrapper, '_msgspec_body_parameter', body.name)
            if body_alias is not None:
                setattr(wrapper, '_msgspec_body_alias', body_alias)
            if callable(schema_callback):
                setattr(
                    wrapper, '_msgspec_body_schema_extra', (schema_callback, deepcopy(field_schema))
                )
        route_count = len(self.routes)
        try:
            result = super().add_api_route(path, wrapper, **kwargs)
            if body is not None and cast(APIRoute, self.routes[-1]).body_field is not None:
                raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')
            return result
        except Exception:
            del self.routes[route_count:]
            raise
