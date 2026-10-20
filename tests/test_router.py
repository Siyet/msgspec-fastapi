"""Tests for MsgspecRouter request decoding, parameter validation and response encoding."""

from typing import Annotated, Any, Generic, TypeVar

import httpx
import httpx2
import msgspec
import pytest
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, Response
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from msgspec_fastapi import MsgspecRouter, install_openapi


class City(msgspec.Struct):
    population: Annotated[int, msgspec.Meta(ge=0)]


class PrivateCity(City):
    secret: str


class PydCity(BaseModel):
    population: Annotated[int, Field(ge=0)]


class AliasModel(BaseModel):
    internal: int = Field(alias='external')


OMITTED = object()


def setup(value: Any, *, model: Any = OMITTED, annotated: bool = False, **options: Any) -> FastAPI:
    router = MsgspecRouter()
    if model is not OMITTED:
        options['response_model'] = model
    if annotated:

        async def endpoint() -> City:
            return value
    else:

        async def endpoint():  # noqa: ANN202 - Test an endpoint without a return annotation.
            return value

    router.add_api_route('/value', endpoint, methods=['GET'], **options)
    app = FastAPI()
    app.include_router(router)
    install_openapi(app, router)
    return app


def response(app: FastAPI, path: str = '/value') -> httpx.Response | httpx2.Response:
    return TestClient(app, raise_server_exceptions=False).get(path)


@pytest.mark.parametrize('kind', ['explicit', 'inferred'])
def test_declared_model_trusts_instance_like_pydantic(kind: str) -> None:
    # Same as FastAPI with a Pydantic response_model: an existing instance of the
    # declared class is not revalidated, so a later mutation is not detected.
    value = City(1)
    value.population = -1
    app = setup(value, model=City if kind == 'explicit' else OMITTED, annotated=kind == 'inferred')
    result = response(app)
    assert result.status_code == 200 and result.json() == {'population': -1}


def test_trusted_instance_encodes_nested_values_as_is() -> None:
    # Documented difference from Pydantic: only the top-level class is checked, so a
    # nested subclass inside a trusted instance keeps its extra fields.
    class Holder(msgspec.Struct):
        city: City

    app = setup(Holder(PrivateCity(1, 'PRIVATE')), model=Holder)
    assert response(app).json() == {'city': {'population': 1, 'secret': 'PRIVATE'}}


def test_trusted_generic_instance_does_not_check_type_parameter() -> None:
    # Documented difference from Pydantic: the type parameter of a generic Struct
    # instance is not checked, only its class.
    T = TypeVar('T')

    class Box(msgspec.Struct, Generic[T]):
        value: T

    app = setup(Box('text'), model=Box[int])
    assert response(app).json() == {'value': 'text'}


@pytest.mark.parametrize('annotated,model', [(True, None), (False, None), (False, OMITTED)])
def test_no_effective_model_serializes_mutated_struct(annotated: bool, model: Any) -> None:
    value = City(1)
    value.population = -1
    result = response(setup(value, model=model, annotated=annotated))
    assert result.status_code == 200 and result.json() == {'population': -1}


@pytest.mark.parametrize('model,status', [(City, 500), (None, 200), (OMITTED, 200)])
def test_raw_dict_constraints_follow_model_selection(model: Any, status: int) -> None:
    result = response(setup({'population': -1}, model=model))
    assert result.status_code == status
    if status == 200:
        assert result.json() == {'population': -1}


def test_no_model_disables_field_projection() -> None:
    value = PrivateCity(1, 'PRIVATE')
    assert response(setup(value, model=City)).json() == {'population': 1}
    assert response(setup(value, model=None, annotated=True)).json() == {
        'population': 1,
        'secret': 'PRIVATE',
    }


@pytest.mark.parametrize(
    'annotated,model,has_schema',
    [(True, OMITTED, True), (False, City, True), (True, None, False), (False, OMITTED, False)],
)
def test_openapi_reflects_effective_response_model(
    annotated: bool, model: Any, has_schema: bool
) -> None:
    app = setup(City(1), annotated=annotated, model=model)
    schema = app.openapi()['paths']['/value']['get']['responses']['200']['content'][
        'application/json'
    ]['schema']
    if has_schema:
        assert schema['$ref'].endswith('/City')
    else:
        assert schema == {}


@pytest.mark.parametrize('model', [City, None, OMITTED])
def test_explicit_response_bypasses_model_processing(model: Any) -> None:
    result = response(
        setup(Response(b'ready', status_code=203, headers={'X-Ready': 'yes'}), model=model)
    )
    assert result.status_code == 203 and result.content == b'ready'
    assert result.headers['X-Ready'] == 'yes'


@pytest.mark.parametrize('model', [City, None, OMITTED])
def test_included_router_keeps_status_and_headers(model: Any) -> None:
    router = MsgspecRouter()
    options = {} if model is OMITTED else {'response_model': model}

    @router.get('/value', status_code=201, **options)
    async def endpoint(response: Response):  # noqa: ANN202 - Test an endpoint without a return annotation.
        response.status_code = 202
        response.headers['X-Endpoint'] = 'kept'
        return City(1)

    parent = MsgspecRouter()
    parent.include_router(router, prefix='/child')
    app = FastAPI()
    app.include_router(parent, prefix='/api')
    install_openapi(app, parent)
    result = response(app, '/api/child/value')
    assert result.status_code == 202 and result.json() == {'population': 1}
    assert result.headers['X-Endpoint'] == 'kept'


@pytest.mark.parametrize('model', [None, OMITTED])
def test_input_validation_remains_enabled_without_output_model(model: Any) -> None:
    router = MsgspecRouter()
    options = {} if model is OMITTED else {'response_model': model}

    @router.post('/value', **options)
    async def endpoint(city: City):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    app = FastAPI()
    app.include_router(router)
    install_openapi(app, router)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post('/value', json={'population': -1}).status_code == 422
    assert (
        client.post(
            '/value', content=b'{', headers={'Content-Type': 'application/json'}
        ).status_code
        == 400
    )
    assert client.post('/value', json={'population': 1}).json() == {'population': 1}


@pytest.mark.parametrize('cls', [PlainTextResponse, HTMLResponse])
@pytest.mark.parametrize('at_router', [False, True])
def test_untyped_custom_response_classes_remain_delegated(
    cls: type[Response], at_router: bool
) -> None:
    router = MsgspecRouter(**({'default_response_class': cls} if at_router else {}))
    options = {} if at_router else {'response_class': cls}

    @router.get('/value', **options)
    async def endpoint():  # noqa: ANN202 - Test an endpoint without a return annotation.
        return '<b>hello</b>'

    app = FastAPI()
    app.include_router(router)
    result = response(app)
    assert result.status_code == 200 and result.text == '<b>hello</b>'
    assert result.headers['Content-Type'].startswith(cls.media_type)


@pytest.mark.parametrize('status', [204, 304])
def test_untyped_bodyless_status_keeps_empty_body(status: int) -> None:
    result = response(setup(None, status_code=status))
    assert result.status_code == status and result.content == b''
    assert 'content-length' not in result.headers


@pytest.mark.parametrize('nested', [False, True])
def test_untyped_pydantic_values_use_public_json_aliases(nested: bool) -> None:
    value = AliasModel(external=1)
    result = response(setup({'items': [value]} if nested else value))
    assert result.status_code == 200
    assert result.json() == ({'items': [{'external': 1}]} if nested else {'external': 1})


def test_annotated_pydantic_route_is_still_delegated() -> None:
    router = MsgspecRouter()

    @router.get('/value')
    async def endpoint() -> PydCity:
        return {'population': -1}

    app = FastAPI()
    app.include_router(router)
    assert response(app).status_code == 500


@pytest.mark.parametrize('model', [None, OMITTED])
@pytest.mark.parametrize(
    'options',
    [
        {'response_model_include': {'population'}},
        {'response_model_exclude': {'extra'}},
        {'response_model_exclude_unset': True},
        {'response_model_exclude_defaults': True},
        {'response_model_exclude_none': True},
        {'response_model_by_alias': False},
    ],
)
def test_without_output_model_ignores_model_specific_options(
    model: Any, options: dict[str, Any]
) -> None:
    value = {'population': 1, 'extra': None}
    result = response(setup(value, model=model, **options))
    assert result.status_code == 200 and result.json() == value


@pytest.mark.parametrize(
    'options',
    [
        {'response_model_exclude_none': True},
        {'response_model_by_alias': False},
    ],
)
def test_typed_response_still_rejects_unsupported_model_options(options: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        setup(City(1), model=City, **options)


def test_non_json_body_is_rejected_before_dependencies() -> None:
    calls = []

    def dependency() -> None:
        calls.append(1)

    router = MsgspecRouter()

    @router.post('/value', dependencies=[Depends(dependency)])
    async def endpoint(city: City):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            '/value', content=b'{', headers={'Content-Type': 'application/json'}
        ).status_code
        == 400
    )
    assert (
        client.post(
            '/value', content=b'population=1', headers={'Content-Type': 'text/plain'}
        ).status_code
        == 415
    )
    assert calls == []
    assert client.post('/value', json={'population': 1}).status_code == 200
    assert calls == [1]


def test_body_is_decoded_strictly() -> None:
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(city: City):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post('/value', json={'population': '1'}).status_code == 422


@pytest.mark.parametrize('prefix', ['', '/api'])
def test_parameters_are_validated_with_msgspec(prefix: str) -> None:
    router = MsgspecRouter()

    @router.get('/value/{item_id}')
    async def endpoint(  # noqa: ANN202 - Test an endpoint without a return annotation.
        item_id: int, limit: Annotated[int, Query(ge=1, alias='max')] = 10, ratio: float = 0.5
    ):
        return {'item_id': item_id, 'limit': limit, 'ratio': ratio}

    app = FastAPI()
    if prefix:
        parent = MsgspecRouter()
        parent.include_router(router)
        app.include_router(parent, prefix=prefix)
        install_openapi(app, parent)
    else:
        app.include_router(router)
        install_openapi(app, router)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f'{prefix}/value/3?max=2&ratio=1.5').json() == {
        'item_id': 3,
        'limit': 2,
        'ratio': 1.5,
    }
    assert client.get(f'{prefix}/value/3').json() == {'item_id': 3, 'limit': 10, 'ratio': 0.5}
    assert client.get(f'{prefix}/value/x').status_code == 422
    assert client.get(f'{prefix}/value/3?max=0').status_code == 422
    # Pydantic accepts surrounding whitespace for float; msgspec.convert does not, so a
    # 422 here shows that msgspec, not Pydantic, validated the included route.
    assert client.get(f'{prefix}/value/3', params={'ratio': ' 1.5'}).status_code == 422
    parameters = app.openapi()['paths'][f'{prefix}/value/{{item_id}}']['get']['parameters']
    assert next(p for p in parameters if p['name'] == 'max')['schema']['minimum'] == 1


def test_install_openapi_requires_routers() -> None:
    with pytest.raises(TypeError):
        install_openapi(FastAPI())


@pytest.mark.parametrize(
    'annotation,value',
    [
        *((int, v) for v in ['5', '+5', '05', '1_0', '5.0', '5.5', ' 5 ', 'x']),
        *((bool, v) for v in ['true', 'yes', 'off', 'f', '1', 'maybe']),
    ],
)
def test_scalar_query_coercion_matches_fastapi(annotation: Any, value: Any) -> None:
    def make(router_class: type[APIRouter]) -> httpx.Response | httpx2.Response:
        router = router_class()

        async def endpoint(q: annotation):  # noqa: ANN202 - Test an endpoint without a return annotation.
            return {'q': q}

        router.add_api_route('/value', endpoint, methods=['GET'])
        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False).get('/value', params={'q': value})

    expected, actual = make(APIRouter), make(MsgspecRouter)
    assert actual.status_code == expected.status_code
    if expected.status_code == 200:
        assert actual.json() == expected.json()


def strict_content_type_app(level: str, strict: bool) -> tuple[TestClient, list[int]]:
    calls = []

    def dependency() -> None:
        calls.append(1)

    async def endpoint(city: City):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    app = FastAPI(**({'strict_content_type': strict} if level == 'app' else {}))
    router = MsgspecRouter(**({'strict_content_type': strict} if level == 'router' else {}))
    router.add_api_route(
        '/value',
        endpoint,
        methods=['POST'],
        dependencies=[Depends(dependency)],
        **({'strict_content_type': strict} if level == 'route' else {}),
    )
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), calls


@pytest.mark.parametrize('level', ['app', 'router', 'route'])
@pytest.mark.parametrize('strict', [True, False])
def test_body_without_content_type_follows_strict_content_type(level: str, strict: bool) -> None:
    client, calls = strict_content_type_app(level, strict)
    result = client.post('/value', content=b'{"population": 1}')
    assert 'content-type' not in result.request.headers
    if strict:
        assert result.status_code == 415 and calls == []
    else:
        assert result.status_code == 200 and result.json() == {'population': 1}


def test_route_strict_content_type_overrides_app() -> None:
    app = FastAPI(strict_content_type=False)
    router = MsgspecRouter()

    async def endpoint(city: City):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    router.add_api_route('/value', endpoint, methods=['POST'], strict_content_type=True)
    app.include_router(router)
    result = TestClient(app, raise_server_exceptions=False).post(
        '/value', content=b'{"population": 1}'
    )
    assert result.status_code == 415


@pytest.mark.parametrize(
    'content_type', ['', ';', 'text/plain', 'application/x-www-form-urlencoded']
)
def test_body_with_non_json_content_type_is_rejected(content_type: str) -> None:
    client, calls = strict_content_type_app('app', False)
    result = client.post(
        '/value', content=b'{"population": 1}', headers={'Content-Type': content_type}
    )
    expected = 200 if content_type == '' else 415
    assert result.status_code == expected
    assert calls == ([1] if expected == 200 else [])


@pytest.mark.parametrize(
    'content_type', ['application/json; charset=utf-8', 'application/merge-patch+json']
)
def test_body_with_json_content_type_is_accepted(content_type: str) -> None:
    client, _ = strict_content_type_app('app', True)
    result = client.post(
        '/value', content=b'{"population": 1}', headers={'Content-Type': content_type}
    )
    assert result.status_code == 200 and result.json() == {'population': 1}


@pytest.mark.parametrize('annotation', [City, list[int], list[Any]])
def test_deeply_nested_json_body_is_rejected(annotation: Any) -> None:
    router = MsgspecRouter()

    async def endpoint(value: annotation):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return {}

    router.add_api_route('/value', endpoint, methods=['POST'])
    app = FastAPI()
    app.include_router(router)
    depth = 200_000
    result = TestClient(app, raise_server_exceptions=False).post(
        '/value', content=b'[' * depth + b']' * depth, headers={'Content-Type': 'application/json'}
    )
    assert result.status_code == 400


class Reading(msgspec.Struct):
    value: float


class LabeledReading(Reading):
    label: str = ''


@pytest.mark.parametrize(
    'annotation,value,expected',
    [
        (float, float('nan'), None),
        (float, float('inf'), None),
        (dict[str, float], {'a': float('-inf')}, {'a': None}),
        (Reading, LabeledReading(float('nan'), 'x'), {'value': None}),
        (list[Reading], [Reading(float('nan'))], [{'value': None}]),
        (dict[str, float], {1: float('nan')}, {'1': None}),
        (dict[int, float], {'1': float('nan')}, {'1': None}),
    ],
)
def test_non_finite_floats_in_typed_response_are_encoded_as_null(
    annotation: Any, value: Any, expected: Any
) -> None:
    # FastAPI with Pydantic also encodes these as null instead of failing.
    result = response(setup(value, model=annotation))
    assert result.status_code == 200 and result.json() == expected


class Measurement(msgspec.Struct):
    value: float
    count: int


@pytest.mark.parametrize(
    'annotation,value,message',
    [
        (Annotated[float, msgspec.Meta(ge=0)], float('nan'), 'Expected `float` >= 0.0'),
        (Annotated[float, msgspec.Meta(ge=0)], float('-inf'), 'Expected `float` >= 0.0'),
        (
            Measurement,
            {'value': float('nan'), 'count': 'x'},
            'Expected `int`, got `str` - at `$.count`',
        ),
        (Reading, {'value': 'x'}, 'Expected `float`, got `str` - at `$.value`'),
        (float, 'nan', 'Expected `float`, got `str`'),
        (float, msgspec.Raw(b'"x"'), 'Expected `float`, got `str`'),
    ],
)
def test_invalid_typed_response_is_still_rejected(
    annotation: Any, value: Any, message: str
) -> None:
    router = MsgspecRouter()

    async def endpoint():  # noqa: ANN202 - Test an endpoint without a return annotation.
        return value

    router.add_api_route('/value', endpoint, methods=['GET'], response_model=annotation)
    app = FastAPI()
    app.include_router(router)
    with pytest.raises(ResponseValidationError) as info:
        TestClient(app).get('/value')
    assert info.value.errors()[0]['msg'] == message


def auth_app() -> tuple[TestClient, list[str | None]]:
    calls = []

    def authenticate(token: Annotated[str | None, Header()] = None) -> None:
        calls.append(token)
        if token != 'secret':
            raise HTTPException(401, 'Not authenticated')

    router = MsgspecRouter()

    @router.post('/value', dependencies=[Depends(authenticate)])
    async def endpoint(city: City):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), calls


@pytest.mark.parametrize('body', [{'population': -1}, {}, None])
def test_body_validation_errors_are_reported_after_dependencies(
    body: dict[str, Any] | None,
) -> None:
    # As in FastAPI, a failing dependency such as authentication takes precedence over
    # a body that does not match the schema.
    client, calls = auth_app()
    options = {} if body is None else {'json': body}
    assert client.post('/value', **options).status_code == 401
    assert calls == [None]
    result = client.post('/value', headers={'token': 'secret'}, **options)
    assert result.status_code == 422
    assert result.json()['detail'][0]['loc'] == ['body']


def test_malformed_body_is_still_rejected_before_dependencies() -> None:
    client, calls = auth_app()
    assert (
        client.post(
            '/value', content=b'{', headers={'Content-Type': 'application/json'}
        ).status_code
        == 400
    )
    assert calls == []


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_none_return_annotation_declares_no_response_model(router_class: type[APIRouter]) -> None:
    router = router_class()

    @router.delete('/value', status_code=204)
    async def delete() -> None:
        pass

    @router.get('/value')
    async def get() -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    deleted = client.delete('/value')
    assert deleted.status_code == 204 and deleted.content == b''
    assert client.get('/value').json() is None
    responses = app.openapi()['paths']['/value']['get']['responses']
    assert responses['200']['content']['application/json']['schema'] == {}


def test_form_and_file_parameters_are_left_to_fastapi() -> None:
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(name: Annotated[str, Form()], data: Annotated[bytes, File()]):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return {'name': name, 'size': len(data)}

    app = FastAPI()
    app.include_router(router)
    result = TestClient(app).post('/value', data={'name': 'x'}, files={'data': b'abc'})
    assert result.status_code == 200 and result.json() == {'name': 'x', 'size': 3}


def test_struct_form_parameter_is_rejected() -> None:
    router = MsgspecRouter()

    async def endpoint(city: Annotated[City, Form()]):  # noqa: ANN202 - Test an endpoint without a return annotation.
        return city

    with pytest.raises(TypeError, match='Form or File'):
        router.add_api_route('/value', endpoint, methods=['POST'])


@pytest.mark.parametrize('annotation,value', [(str, '\ud800'), (Any, {None: 1})])
def test_unencodable_typed_response_raises_response_validation_error(
    annotation: Any, value: Any
) -> None:
    router = MsgspecRouter()

    async def endpoint():  # noqa: ANN202 - Test an endpoint without a return annotation.
        return value

    router.add_api_route('/value', endpoint, methods=['GET'], response_model=annotation)
    app = FastAPI()
    app.include_router(router)
    with pytest.raises(ResponseValidationError):
        TestClient(app).get('/value')
