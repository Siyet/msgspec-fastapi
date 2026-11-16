"""OpenAPI coverage with ordinary FastAPI routes as compatibility controls."""

from enum import Enum
from typing import Annotated, Any

import msgspec
import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import BaseModel, create_model

from msgspec_fastapi import MsgspecRoute, MsgspecRouter, install_openapi


class Choice(str, Enum):
    first = 'first'
    second = 'second'


class Item(msgspec.Struct):
    choice: Choice


class PydItem(BaseModel):
    choice: Choice


def assert_local_refs_resolve(document: dict[str, Any]) -> None:
    def resolve(ref: str) -> None:
        assert ref.startswith('#/'), ref
        value = document
        for part in ref[2:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            assert part in value, ref
            value = value[part]

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('examples', 'example', 'default', 'const', 'enum'):
                    continue
                if key == '$ref' and isinstance(child, str):
                    resolve(child)
                else:
                    walk(child)
            for ref in value.get('discriminator', {}).get('mapping', {}).values():
                resolve(ref)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)


def dereference(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if '$ref' not in schema:
        return schema
    return document['components']['schemas'][schema['$ref'].rsplit('/', 1)[-1]]


@pytest.mark.parametrize('router_class,model', [(MsgspecRouter, Item), (APIRouter, PydItem)])
@pytest.mark.parametrize('with_body', [False, True])
def test_enum_parameter_and_model_references(
    router_class: type[APIRouter], model: Any, with_body: bool
) -> None:
    router = router_class()

    async def endpoint(choice: Choice) -> Any:
        return {'choice': choice}

    router.add_api_route(
        '/items', endpoint, methods=['GET'], response_model=model if with_body else None
    )
    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    response = TestClient(app).get('/openapi.json')
    assert response.status_code == 200
    document = response.json()
    assert_local_refs_resolve(document)
    parameter = document['paths']['/items']['get']['parameters'][0]['schema']
    assert dereference(document, parameter)['enum'] == ['first', 'second']
    assert TestClient(app).get('/items?choice=first').json() == {'choice': 'first'}
    assert TestClient(app).get('/items?choice=other').status_code == 422


@pytest.mark.parametrize('pydantic', [False, True])
def test_same_named_models_from_different_modules(pydantic: bool) -> None:
    router = APIRouter() if pydantic else MsgspecRouter()
    for module, field in [('module_a', 'number'), ('module_b', 'count')]:
        model = (
            create_model('Entry', __module__=module, **{field: (int, ...)})
            if pydantic
            else msgspec.defstruct('Entry', [(field, int)], module=module)
        )

        async def endpoint() -> Any:
            return {}

        router.add_api_route('/' + module, endpoint, response_model=model)
    app = FastAPI()
    app.include_router(router)
    if not pydantic:
        install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    for module, field in [('module_a', 'number'), ('module_b', 'count')]:
        schema = document['paths']['/' + module]['get']['responses']['200']['content'][
            'application/json'
        ]['schema']
        assert set(dereference(document, schema)['properties']) == {field}


def test_pydantic_and_msgspec_components_with_same_name() -> None:
    pyd_model = create_model('Item', count=(int, ...))
    ordinary = APIRouter()

    @ordinary.get('/pydantic', response_model=pyd_model)
    async def pydantic_endpoint() -> Any:
        return {'count': 1}

    router = MsgspecRouter()

    @router.post('/msgspec')
    async def msgspec_endpoint(item: Item) -> Item:
        return item

    app = FastAPI()
    app.include_router(ordinary)
    app.include_router(router)
    install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    for path, method, field in [('/pydantic', 'get', 'count'), ('/msgspec', 'post', 'choice')]:
        schema = document['paths'][path][method]['responses']['200']['content']['application/json'][
            'schema'
        ]
        assert set(dereference(document, schema)['properties']) == {field}


@pytest.mark.parametrize('arguments', ['none', 'partial', 'plain_parent'])
def test_discovery_does_not_depend_on_router_arguments(arguments: str) -> None:
    first = MsgspecRouter()
    second = MsgspecRouter()

    @second.post('/items')
    async def endpoint(item: Item) -> Item:
        return item

    parent = APIRouter()
    parent.include_router(second, prefix='/child')
    app = FastAPI()
    app.include_router(first)
    app.include_router(parent, prefix='/api')
    install_openapi(
        app, *([] if arguments == 'none' else [first] if arguments == 'partial' else [parent])
    )
    document = app.openapi()
    assert '/api/child/items' in document['paths']
    assert_local_refs_resolve(document)
    assert TestClient(app).post('/api/child/items', json={'choice': 'first'}).json() == {
        'choice': 'first'
    }


def test_plain_router_with_msgspec_route_and_dependency_parameters() -> None:
    router = APIRouter(route_class=MsgspecRoute)

    def dependency(choice: Annotated[Choice, Header()]) -> Choice:
        return choice

    @router.get('/items')
    async def endpoint(choice: Annotated[Choice, Depends(dependency)]) -> Any:
        return {'choice': choice}

    app = FastAPI()
    app.include_router(router)
    install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    schema = document['paths']['/items']['get']['parameters'][0]['schema']
    assert dereference(document, schema)['enum'] == ['first', 'second']


def test_late_routes_and_openapi_cache() -> None:
    router = MsgspecRouter()
    app = FastAPI()
    app.include_router(router, prefix='/api')
    install_openapi(app)

    @router.post('/items')
    async def endpoint(item: Item) -> Item:
        return item

    first = app.openapi()
    assert '/api/items' in first['paths']
    assert app.openapi() is first
    assert_local_refs_resolve(first)
    router.add_api_route('/more', endpoint, methods=['POST'])
    assert '/api/more' not in app.openapi()['paths']
    app.openapi_schema = None
    second = app.openapi()
    assert '/api/more' in second['paths']
    assert_local_refs_resolve(second)


@pytest.mark.parametrize('kind', ['route', 'included'])
def test_hidden_routes_do_not_add_components(kind: str) -> None:
    class Hidden(msgspec.Struct):
        value: str

    router = MsgspecRouter()

    @router.post('/hidden', include_in_schema=kind != 'route')
    async def endpoint(item: Hidden) -> Hidden:
        return item

    app = FastAPI()
    app.include_router(router, include_in_schema=kind != 'included')
    install_openapi(app, router)
    document = app.openapi()
    assert document['paths'] == {}
    assert not document.get('components', {}).get('schemas')


def test_webhooks_and_nested_callbacks() -> None:
    callbacks = MsgspecRouter()
    nested = MsgspecRouter()

    @nested.post('/nested')
    async def nested_callback(item: Item, choice: Choice) -> Item:
        return item

    @callbacks.post('/callback', callbacks=nested.routes)
    async def callback(item: Item, choice: Choice) -> Item:
        return item

    webhook = MsgspecRouter()

    @webhook.post('event')
    async def event(item: Item, choice: Choice) -> Item:
        return item

    router = APIRouter()

    @router.post('/trigger', callbacks=callbacks.routes)
    async def trigger() -> None:
        pass

    app = FastAPI(webhooks=webhook)
    app.include_router(router)
    install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    webhook_op = document['webhooks']['event']['post']
    callback_op = document['paths']['/trigger']['post']['callbacks']['callback']['/callback'][
        'post'
    ]
    nested_op = callback_op['callbacks']['nested_callback']['/nested']['post']
    for operation in (webhook_op, callback_op, nested_op):
        schema = operation['requestBody']['content']['application/json']['schema']
        assert set(dereference(document, schema)['properties']) == {'choice'}
        parameter = operation['parameters'][0]['schema']
        assert dereference(document, parameter)['enum'] == ['first', 'second']
        assert '415' in operation['responses']


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_body_and_response_metadata_matches_fastapi(router_class: type[APIRouter]) -> None:
    router = router_class()
    examples = {'sample': {'summary': 'An example', 'value': 'first'}}

    @router.post(
        '/items',
        responses={
            200: {
                'description': 'Custom result',
                'content': {
                    'application/json': {'examples': examples},
                    'text/plain': {'example': 'first'},
                },
            }
        },
        openapi_extra={
            'requestBody': {
                'description': 'Request description',
                'x-custom': True,
                'content': {'application/json': {'example': 'second'}},
            }
        },
    )
    async def endpoint(
        item: Annotated[
            str,
            Body(
                description='Body description',
                examples=['first'],
                openapi_examples=examples,
                json_schema_extra={'x-schema': True},
            ),
        ],
    ) -> str:
        return item

    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    document = app.openapi()
    operation = document['paths']['/items']['post']
    request = operation['requestBody']
    assert request['required'] is True
    assert request['description'] == 'Request description' and request['x-custom'] is True
    media = request['content']['application/json']
    assert media['example'] == 'second' and media['examples'] == examples
    assert media['schema']['description'] == 'Body description'
    assert media['schema']['examples'] == ['first'] and media['schema']['x-schema'] is True
    assert media['schema']['type'] == 'string'
    result = operation['responses']['200']
    assert result['description'] == 'Custom result'
    assert result['content']['application/json']['examples'] == examples
    assert result['content']['text/plain']['example'] == 'first'
    assert result['content']['application/json']['schema']['type'] == 'string'


@pytest.mark.parametrize('annotation', [Item, list[int], Annotated[list[int], Body()]])
def test_structured_path_parameter_fails_at_registration(annotation: Any) -> None:
    router = MsgspecRouter()

    async def endpoint(item: annotation) -> None:
        pass

    with pytest.raises(AssertionError, match='Path parameters'):
        router.add_api_route('/{item}', endpoint, methods=['POST'])
    assert router.routes == []


def test_structured_path_parameter_in_include_prefix_is_rejected() -> None:
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(item: Item) -> None:
        pass

    app = FastAPI()
    app.include_router(router, prefix='/{item}')
    with pytest.raises(AssertionError, match='Path parameters'):
        install_openapi(app)


def test_415_documentation_and_user_override() -> None:
    router = MsgspecRouter()

    @router.post('/items', responses={415: {'description': 'Custom media error'}})
    async def endpoint(item: Item) -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    install_openapi(app)
    operation = app.openapi()['paths']['/items']['post']
    assert operation['responses']['415']['description'] == 'Custom media error'
    assert (
        TestClient(app)
        .post('/items', content='{}', headers={'Content-Type': 'text/plain'})
        .status_code
        == 415
    )


def test_install_twice_is_rejected() -> None:
    app = FastAPI()
    install_openapi(app)
    with pytest.raises(RuntimeError, match='already installed'):
        install_openapi(app)


def test_nested_collisions_and_literal_refs_are_preserved() -> None:
    pyd_child = create_model('Child', literal=(str, ...))
    pyd_parent = create_model('Parent', child=(pyd_child, ...))
    example = {'$ref': '#/components/schemas/Child'}
    child = msgspec.defstruct(
        'Child',
        [('ref', str), ('literal', Annotated[dict[str, str], msgspec.Meta(examples=[example])])],
        rename={'ref': '$ref'},
    )
    parent = msgspec.defstruct('Parent', [('child', child)])
    app = FastAPI()
    plain = APIRouter()
    router = MsgspecRouter()

    async def endpoint() -> Any:
        return {}

    plain.add_api_route('/plain', endpoint, response_model=pyd_parent)
    router.add_api_route('/msgspec', endpoint, response_model=parent)
    app.include_router(plain)
    app.include_router(router)
    install_openapi(app)
    document = app.openapi()
    schema = document['paths']['/msgspec']['get']['responses']['200']['content'][
        'application/json'
    ]['schema']
    assert_local_refs_resolve(document)
    nested = dereference(document, dereference(document, schema)['properties']['child'])
    assert nested['properties']['$ref']['type'] == 'string'
    assert nested['properties']['literal']['examples'] == [example]
    original = document['paths']['/plain']['get']['responses']['200']['content'][
        'application/json'
    ]['schema']
    original_child = dereference(document, dereference(document, original)['properties']['child'])
    assert original_child['properties']['literal']['type'] == 'string'


@pytest.mark.parametrize('first_plain,last_plain', [(False, False), (False, True), (True, False)])
def test_duplicate_operations_use_last_visible_route(first_plain: bool, last_plain: bool) -> None:
    app = FastAPI()
    first = APIRouter() if first_plain else MsgspecRouter()
    last = APIRouter() if last_plain else MsgspecRouter()
    first_model = (
        create_model('First', first=(int, ...))
        if first_plain
        else msgspec.defstruct('First', [('first', int)])
    )
    last_model = (
        create_model('Last', last=(str, ...))
        if last_plain
        else msgspec.defstruct('Last', [('last', str)])
    )

    async def endpoint_first(item: first_model) -> first_model:
        return item

    async def endpoint_last(item: last_model) -> last_model:
        return item

    first.add_api_route('/items', endpoint_first, methods=['POST'])
    last.add_api_route('/items', endpoint_last, methods=['POST'])
    app.include_router(first)
    app.include_router(last)
    install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    operation = document['paths']['/items']['post']
    schemas = [
        operation['requestBody']['content']['application/json']['schema'],
        operation['responses']['200']['content']['application/json']['schema'],
    ]
    for schema in schemas:
        assert set(dereference(document, schema)['properties']) == {'last'}


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_embedded_body_metadata_belongs_to_field(router_class: type[APIRouter]) -> None:
    router = router_class()
    for path, description, examples in [
        ('/first', 'First field', [12]),
        ('/second', 'Second field', [24]),
    ]:

        async def endpoint(
            item: Annotated[
                int,
                Body(
                    embed=True,
                    alias='number',
                    title='Field title',
                    description=description,
                    examples=examples,
                    json_schema_extra={'x-field': True},
                ),
            ],
        ) -> int:
            return item

        router.add_api_route(path, endpoint, methods=['POST'])
    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    for path, description, examples in [
        ('/first', 'First field', [12]),
        ('/second', 'Second field', [24]),
    ]:
        schema = document['paths'][path]['post']['requestBody']['content']['application/json'][
            'schema'
        ]
        envelope = dereference(document, schema)
        assert 'description' not in schema and 'examples' not in schema
        field = envelope['properties']['number']
        assert field['type'] == 'integer'
        assert field['title'] == 'Field title'
        assert field['description'] == description
        assert field['examples'] == examples
        assert field['x-field'] is True
        result = TestClient(app).post(path, json={'number': 12})
        assert result.status_code == 200 and result.json() == 12


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('embedded', [False, True])
def test_body_schema_callable_matches_fastapi(
    router_class: type[APIRouter], embedded: bool
) -> None:
    router = router_class()
    calls = []

    def patch(schema: dict[str, Any]) -> None:
        calls.append(schema.copy())
        assert schema['type'] == 'integer' and schema['title'] == 'Field title'
        schema['description'] = 'Callback description'
        schema['examples'] = [42]

    @router.post(
        '/items',
        openapi_extra={
            'requestBody': {'content': {'application/json': {'schema': {'x-operation': True}}}}
        },
    )
    async def endpoint(
        item: Annotated[
            int,
            Body(
                embed=embedded,
                alias='number',
                title='Field title',
                description='Original description',
                json_schema_extra=patch,
            ),
        ],
    ) -> int:
        return item

    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    assert not calls
    assert TestClient(app).post('/items', json={'number': 7} if embedded else 7).json() == 7
    assert not calls
    document = app.openapi()
    schema = document['paths']['/items']['post']['requestBody']['content']['application/json'][
        'schema'
    ]
    assert schema['x-operation'] is True
    field = dereference(document, schema)['properties']['number'] if embedded else schema
    assert field['description'] == 'Callback description' and field['examples'] == [42]
    count = len(calls)
    assert count > 0
    assert app.openapi() is document and len(calls) == count


def test_embedded_schema_callbacks_are_isolated() -> None:
    router = MsgspecRouter()
    for path, example in [('/first', 12), ('/second', 24)]:

        def patch(schema: dict[str, Any], value: int = example) -> None:
            schema['examples'] = [value]

        async def endpoint(item: Annotated[int, Body(embed=True, json_schema_extra=patch)]) -> int:
            return item

        router.add_api_route(path, endpoint, methods=['POST'])
    app = FastAPI()
    app.include_router(router)
    install_openapi(app)
    document = app.openapi()
    assert_local_refs_resolve(document)
    for path, example in [('/first', 12), ('/second', 24)]:
        schema = document['paths'][path]['post']['requestBody']['content']['application/json'][
            'schema'
        ]
        assert dereference(document, schema)['properties']['item']['examples'] == [example]


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('embedded', [False, True])
def test_body_schema_dictionary_extra_overrides_metadata(
    router_class: type[APIRouter], embedded: bool
) -> None:
    router = router_class()

    @router.post('/items')
    async def endpoint(
        item: Annotated[
            int, Body(embed=embedded, examples=[1], json_schema_extra={'examples': [2]})
        ],
    ) -> int:
        return item

    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    document = app.openapi()
    schema = document['paths']['/items']['post']['requestBody']['content']['application/json'][
        'schema'
    ]
    field = dereference(document, schema)['properties']['item'] if embedded else schema
    assert field['examples'] == [2]


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_optional_embedded_callback_receives_default(router_class: type[APIRouter]) -> None:
    router = router_class()

    def patch(schema: dict[str, Any]) -> None:
        schema['examples'] = [schema['default']]

    @router.post('/items')
    async def endpoint(
        item: Annotated[int, Body(embed=True, alias='number', json_schema_extra=patch)] = 7,
    ) -> int:
        return item

    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    document = app.openapi()
    body = document['paths']['/items']['post']['requestBody']
    schema = dereference(document, body['content']['application/json']['schema'])
    assert not body.get('required') and 'required' not in schema
    assert schema['properties']['number']['default'] == 7
    assert schema['properties']['number']['examples'] == [7]
    assert TestClient(app).post('/items', json={}).json() == 7


def test_failed_schema_callback_does_not_cache_partial_document() -> None:
    attempts = 0

    def patch(schema: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            schema['x-partial'] = True
            raise RuntimeError('Schema callback failed')

    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(item: Annotated[int, Body(json_schema_extra=patch)]) -> int:
        return item

    app = FastAPI()
    app.include_router(router)
    install_openapi(app)
    with pytest.raises(RuntimeError, match='Schema callback failed'):
        app.openapi()
    assert app.openapi_schema is None
    document = app.openapi()
    schema = document['paths']['/items']['post']['requestBody']['content']['application/json'][
        'schema'
    ]
    assert schema['type'] == 'integer' and 'x-partial' not in schema
    assert attempts == 2 and app.openapi() is document
