from __future__ import annotations

import inspect
from copy import deepcopy
from functools import partial, wraps
from hashlib import sha256
from typing import Annotated, get_args, get_origin, get_type_hints

import msgspec
from pydantic import BaseModel
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.datastructures import DefaultPlaceholder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.utils import validation_error_definition, validation_error_response_definition
from fastapi.params import Body, Depends, Param
from fastapi.routing import APIRoute, _effective_route_context_var, iter_route_contexts
from starlette.concurrency import run_in_threadpool

from .responses import MsgspecJSONResponse
from .validation import base_type, constrained_type, prepare_parameters, structured_type, uses_pydantic


def _untyped_encode_hook(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json', by_alias=True)
    raise TypeError(f'Encoding objects of type {type(value).__name__} is unsupported')


def _contains_struct(annotation):
    value = base_type(annotation)
    return (isinstance(value, type) and issubclass(value, msgspec.Struct)) or any(
        _contains_struct(arg) for arg in get_args(value)
    )


def _body_type(annotation):
    value = base_type(annotation)
    if structured_type(value):
        return True
    if get_origin(value) in (list, dict, tuple, set, frozenset):
        return True
    return any(_body_type(arg) for arg in get_args(value))


def _metadata(parameter, annotation):
    items = list(get_args(annotation)[1:]) if get_origin(annotation) is Annotated else []
    return [*items, parameter.default]


def _merge_components(target, incoming):
    for name, schema in incoming.items():
        if name in target and target[name] != schema:
            raise ValueError(f'Conflicting OpenAPI component: {name}')
        target[name] = deepcopy(schema)


def _schema(annotation, components):
    (schema,), definitions = msgspec.json.schema_components(
        [annotation], ref_template='#/components/schemas/{name}'
    )
    _merge_components(components, definitions)
    return schema


def _json_type(annotation):
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


def _hints(endpoint):
    target = endpoint
    while isinstance(target, partial):
        target = target.func
    if not inspect.isfunction(target) and not inspect.ismethod(target):
        target = target.__call__
    return get_type_hints(target, include_extras=True)


def _parse_body(request, raw, config):
    decoder, default, embedded = config
    if not raw:
        if default is inspect.Signature.empty:
            raise RequestValidationError([{'loc': ('body',), 'msg': 'Field required', 'type': 'missing'}])
        return deepcopy(default)
    media_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if media_type and media_type != 'application/json' and not (
        media_type.startswith('application/') and media_type.endswith('+json')
    ):
        raise HTTPException(415, 'Expected a JSON request body')
    try:
        value = decoder.decode(raw)
    except msgspec.ValidationError as exc:
        # Typed decoding can stop on a field error before discovering invalid syntax.
        try:
            msgspec.json.decode(raw)
        except (msgspec.DecodeError, UnicodeError) as syntax:
            raise HTTPException(400, f'Malformed JSON body: {syntax}') from syntax
        raise RequestValidationError([{'loc': ('body',), 'msg': str(exc), 'type': 'value_error.msgspec'}]) from exc
    except (msgspec.DecodeError, UnicodeError) as exc:
        raise HTTPException(400, f'Malformed JSON body: {exc}') from exc
    return value.value if embedded else value


class MsgspecRoute(APIRoute):
    def get_route_handler(self):
        context = _effective_route_context_var.get()
        route = context if context is not None and context.original_route is self else self
        prepare_parameters(route.dependant)
        handler = super().get_route_handler()
        config = getattr(self.endpoint, '_msgspec_body_config', None)
        if config is None:
            return handler
        if route.body_field is not None:
            raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')

        async def handle(request):
            # Invalid JSON must be rejected before dependency side effects.
            request.scope['_msgspec_body_value'] = _parse_body(request, await request.body(), config)
            return await handler(request)
        return handle


def install_openapi(app: FastAPI, *routers: 'MsgspecRouter') -> None:
    """Install once after including routers, before serving the application."""
    if not routers:
        raise TypeError('Pass the MsgspecRouter instances included in the app')
    if getattr(app, '_msgspec_openapi_installed', False):
        raise RuntimeError('msgspec OpenAPI is already installed')
    # FastAPI builds included contexts lazily; validate them before serving.
    list(iter_route_contexts(app.routes))
    original = app.openapi
    app.openapi_schema = None

    def openapi():
        document = original()
        components = document.setdefault('components', {}).setdefault('schemas', {})
        for router in routers:
            _merge_components(components, router.msgspec_components)
        for route in iter_route_contexts(app.routes):
            original_route = getattr(route, 'original_route', route)
            if not isinstance(original_route, MsgspecRoute):
                continue
            fields = []
            def collect(dependant):
                for group in ('path_params', 'query_params', 'header_params', 'cookie_params'):
                    fields.extend(getattr(dependant, group))
                for dependency in dependant.dependencies:
                    collect(dependency)
            collect(route.dependant)
            for method in route.methods:
                operation = document.get('paths', {}).get(route.path_format, {}).get(method.lower(), {})
                for parameter in operation.get('parameters', []):
                    for field in fields:
                        if (getattr(field, '_msgspec_parameter', False)
                            and parameter['in'] == field.field_info.in_.value
                            and parameter['name'] == field.alias):
                            schema = deepcopy(field._msgspec_schema)
                            for key in ('title', 'description', 'default'):
                                if key in parameter.get('schema', {}):
                                    schema[key] = parameter['schema'][key]
                            parameter['schema'] = schema
        return document

    app.openapi = openapi
    app._msgspec_openapi_installed = True


class MsgspecRouter(APIRouter):
    """Typed msgspec JSON bodies and validated responses for ordinary CRUD routes."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('route_class', MsgspecRoute)
        kwargs.setdefault('default_response_class', MsgspecJSONResponse)
        if not issubclass(kwargs['route_class'], MsgspecRoute):
            raise TypeError('route_class must extend MsgspecRoute')
        super().__init__(*args, **kwargs)
        self.msgspec_components = {}

    def include_router(self, router, *args, **kwargs):
        if isinstance(router, MsgspecRouter):
            _merge_components(self.msgspec_components, router.msgspec_components)
        return super().include_router(router, *args, **kwargs)

    def add_api_route(self, path, endpoint, **kwargs):
        override = kwargs.get('route_class_override')
        if override is not None and not issubclass(override, MsgspecRoute):
            raise TypeError('route_class_override must extend MsgspecRoute')
        # Included routes already have a prepared signature and response metadata.
        if getattr(endpoint, '_msgspec_prepared', False):
            return super().add_api_route(path, endpoint, **kwargs)
        signature = inspect.signature(endpoint)
        hints = _hints(endpoint)
        parameters = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in signature.parameters.values()]
        return_type = hints.get('return', signature.return_annotation)
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
        output_class = (get_origin(base_type(output_type)) or base_type(output_type)) if validates_output else None
        trusts_instances = isinstance(output_class, type) and issubclass(output_class, msgspec.Struct)
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
        handles_output = validates_output or serializes_untyped_output or (
            explicit is None and _contains_struct(return_type)
        )
        untyped_encoder = (
            msgspec.json.Encoder(enc_hook=_untyped_encode_hook)
            if serializes_untyped_output else None
        )
        body = None
        body_meta = None
        for parameter in parameters:
            metadata = _metadata(parameter, parameter.annotation)
            if any(isinstance(x, Depends) for x in metadata):
                continue
            is_body = _contains_struct(parameter.annotation) or any(isinstance(x, Body) for x in metadata)
            if _body_type(parameter.annotation) and not any(isinstance(x, Param) for x in metadata):
                is_body = True
            if is_body and _json_type(parameter.annotation):
                if any(isinstance(x, Param) for x in metadata):
                    raise TypeError('msgspec Struct query/path/header/cookie parameters are not supported')
                if body is not None:
                    raise TypeError('Only one msgspec body parameter is supported; other body fields cannot be combined')
                body = parameter
                body_meta = next((x for x in metadata if isinstance(x, Body)), None)

        if body is None and not handles_output:
            return super().add_api_route(path, endpoint, **kwargs)
        if any(p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL) for p in parameters):
            raise TypeError('Variadic msgspec endpoints are not supported')
        if handles_output:
            if validates_output:
                for option in ('response_model_include', 'response_model_exclude',
                               'response_model_exclude_unset', 'response_model_exclude_defaults',
                               'response_model_exclude_none'):
                    if kwargs.get(option):
                        raise TypeError(f'{option} is not supported for msgspec responses')
                if kwargs.get('response_model_by_alias', True) is False:
                    raise TypeError('msgspec responses use encoded field names')
            if validates_output and kwargs.get('status_code') in (204, 304):
                raise TypeError('A typed msgspec response requires a response body')
            response_class = kwargs.get('response_class')
            if response_class is not None and not isinstance(response_class, DefaultPlaceholder) and response_class is not MsgspecJSONResponse:
                raise TypeError('Custom response_class is not supported for typed msgspec responses')
            kwargs['response_model'] = None
            kwargs['response_class'] = MsgspecJSONResponse

        components = {}
        responses = deepcopy(kwargs.get('responses') or {})
        openapi_extra = deepcopy(kwargs.get('openapi_extra') or {})
        default = inspect.Signature.empty
        decoder = None
        embedded = False
        if body is not None:
            metadata = [x for x in get_args(body.annotation)[1:] if not isinstance(x, Body)] if get_origin(body.annotation) is Annotated else []
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
                if not isinstance(body.default, Body) and body.default is not inspect.Signature.empty:
                    default = body.default
                if body_meta.default_factory is not None:
                    raise TypeError('Body default_factory is not supported')
            else:
                default = body.default
            decode_type = body_type
            if embedded:
                # One explicit JSON envelope, preserving a possible Body alias.
                field = ('value', body_type) if default is inspect.Signature.empty else (
                    'value', body_type, msgspec.field(default_factory=lambda: deepcopy(default))
                )
                identity = {'schema': msgspec.json.schema(body_type), 'alias': body_meta.alias or body.name,
                            'required': default is inspect.Signature.empty}
                if default is not inspect.Signature.empty:
                    identity['default'] = default
                name = 'MsgspecBody_' + sha256(msgspec.json.encode(identity)).hexdigest()[:16]
                decode_type = msgspec.defstruct(name, [field],
                                               rename={'value': body_meta.alias or body.name})
            decoder = msgspec.json.Decoder(type=decode_type)
            body_schema = _schema(decode_type, components)
            openapi_extra['requestBody'] = {'required': default is inspect.Signature.empty,
                'content': {'application/json': {'schema': body_schema}}}
            responses.setdefault(400, {'description': 'Malformed JSON'})
            responses.setdefault(422, {'description': 'Validation Error', 'content': {
                'application/json': {'schema': {'$ref': '#/components/schemas/HTTPValidationError'}}}})
            _merge_components(components, {'HTTPValidationError': validation_error_response_definition,
                                           'ValidationError': validation_error_definition})
        if validates_output:
            code = kwargs.get('status_code') or 200
            key = str(code) if str(code) in responses else code
            result_meta = responses.setdefault(key, {})
            result_meta.setdefault('description', kwargs.get('response_description', 'Successful Response'))
            result_meta.setdefault('content', {})['application/json'] = {'schema': _schema(output_type, components)}
        previous_components = deepcopy(self.msgspec_components)
        _merge_components(self.msgspec_components, components)
        kwargs['responses'] = responses
        kwargs['openapi_extra'] = openapi_extra

        remaining = [p for p in parameters if p is not body]
        def injected_name(type_, fallback):
            for p in remaining:
                annotation = base_type(p.annotation)
                if isinstance(annotation, type) and issubclass(annotation, type_):
                    return p.name, False
            names = {p.name for p in parameters}
            while fallback in names:
                fallback += '_'
            remaining.append(inspect.Parameter(fallback, kind=inspect.Parameter.KEYWORD_ONLY, annotation=type_))
            return fallback, True
        request_name, remove_request = injected_name(Request, '__msgspec_request') if body else (None, False)
        response_name, remove_response = injected_name(Response, '__msgspec_response') if handles_output else (None, False)

        is_async = inspect.iscoroutinefunction(endpoint) or inspect.iscoroutinefunction(getattr(endpoint, '__call__', None))
        @wraps(endpoint)
        async def wrapper(**values):
            sub_response = values.get(response_name)
            if remove_response:
                values.pop(response_name)
            if body is not None:
                request = values[request_name]
                if remove_request:
                    values.pop(request_name)
                value = request.scope['_msgspec_body_value']
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
                    result = output_decoder.decode(output_encoder.encode(result))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ResponseValidationError([{'loc': ('response',), 'msg': str(exc), 'type': 'value_error.msgspec'}]) from exc
            status_code = sub_response.status_code or kwargs.get('status_code') or 200
            if untyped_encoder is not None:
                response = Response(untyped_encoder.encode(result), status_code=status_code,
                                    media_type='application/json')
            else:
                response = MsgspecJSONResponse(result, status_code=status_code)
            if response.status_code < 200 or response.status_code in (204, 205, 304):
                response.body = b''
                if 'content-length' in response.headers:
                    del response.headers['content-length']
            response.raw_headers.extend(sub_response.raw_headers)
            return response

        wrapper.__signature__ = signature.replace(parameters=remaining, return_annotation=inspect.Signature.empty if handles_output else return_type)
        wrapper._msgspec_prepared = True
        if body is not None:
            wrapper._msgspec_body_config = (decoder, default, embedded)
        route_count = len(self.routes)
        try:
            result = super().add_api_route(path, wrapper, **kwargs)
            if body is not None and self.routes[-1].body_field is not None:
                raise TypeError('Other FastAPI body fields cannot be combined with a msgspec body')
            return result
        except Exception:
            del self.routes[route_count:]
            self.msgspec_components = previous_components
            raise

