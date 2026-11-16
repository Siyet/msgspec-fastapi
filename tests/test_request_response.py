"""Request/response compatibility controls for improvement-plan step 3."""

import ipaddress
import json
import re
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import msgspec
import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, HttpUrl, SecretStr
from pydantic_core import SchemaError, SchemaValidator
from regex_rs import RegexError

from msgspec_fastapi import MsgspecRouter, install_openapi


class Item(msgspec.Struct):
    value: int


class PydItem(BaseModel):
    value: int


def application(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if isinstance(router, MsgspecRouter):
        install_openapi(app)
    return app


def assert_sse_rejected(
    app: FastAPI, message: str = 'Use ServerSentEvent(raw_data=...) for event data'
) -> None:
    with pytest.raises((TypeError, ExceptionGroup)) as caught:
        TestClient(app).get('/value')

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]

    errors = leaves(caught.value)
    assert len(errors) == 1 and isinstance(errors[0], TypeError)
    assert str(errors[0]) == message


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('where', ['query', 'body', 'list', 'set', 'dict', 'nullable', 'tuple'])
def test_string_patterns_match_pydantic(router_class: type[APIRouter], where: str) -> None:
    router = router_class()
    pattern = Annotated[str, Field(pattern='^a+$')]
    annotation = {
        'query': Annotated[str, Query(pattern='^a+$')],
        'body': Annotated[str, Body(pattern='^a+$')],
        'list': list[pattern],
        'set': set[pattern],
        'dict': dict[pattern, pattern],
        'nullable': Annotated[pattern | None, Body()],
        'tuple': tuple[pattern, pattern],
    }[where]

    async def endpoint(value: annotation) -> Any:
        return value

    router.add_api_route('/value', endpoint, methods=['POST'])
    app = application(router)
    client = TestClient(app)
    if where == 'query':
        result = client.post('/value', params={'value': 'a\n'})
    else:
        invalid = {
            'body': 'a\n',
            'list': ['aa', 'a\n', 'bad'],
            'set': ['aa', 'bad', 'bad'],
            'dict': {'bad': 'a\n'},
            'nullable': 'a\n',
            'tuple': ['aa', 'a\n'],
        }[where]
        result = client.post('/value', json=invalid)
    assert result.status_code == 422
    locations = [error['loc'] for error in result.json()['detail']]
    expected = {
        'query': [['query', 'value']],
        'body': [['body']],
        'list': [['body', 1], ['body', 2]],
        'set': [['body', 1], ['body', 2]],
        'dict': [['body', 'bad', '[key]'], ['body', 'bad']],
        'nullable': [['body']],
        'tuple': [['body', 1]],
    }[where]
    assert locations == expected
    if where == 'nullable':
        assert client.post('/value', json=None).status_code == 422  # An absent required body.


def test_plain_fastapi_compiled_pattern_keeps_explicit_python_semantics() -> None:
    router = APIRouter()

    @router.get('/value')
    async def endpoint(value: Annotated[str, Query(pattern=re.compile('^a$', re.I))]) -> str:
        return value

    assert TestClient(application(router)).get('/value', params={'value': 'A\n'}).json() == 'A\n'


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_string_pattern_unsupported_by_rust_is_rejected(router_class: type[APIRouter]) -> None:
    router = router_class()

    async def endpoint(value: Annotated[str, Query(pattern='(?<=a)b')]) -> str:
        return value

    with pytest.raises((SchemaError, RegexError)):
        router.add_api_route('/value', endpoint)


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_untyped_non_native_values_follow_router_policy(router_class: type[APIRouter]) -> None:
    router = router_class()

    @router.get('/value')
    async def endpoint():  # noqa: ANN202 - Exercise an untyped response.
        return {
            'path': Path('/tmp/a'),
            'ip': ipaddress.ip_address('127.0.0.1'),
            'url': HttpUrl('https://example.com'),
            'secret': SecretStr('private'),
        }

    if router_class is MsgspecRouter:
        result = TestClient(application(router), raise_server_exceptions=False).get('/value')
        assert result.status_code == 500
        assert 'private' not in result.text
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.json() == {
        'path': str(Path('/tmp/a')),
        'ip': '127.0.0.1',
        'url': 'https://example.com/',
        'secret': '**********',
    }


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_oversized_integer_error_does_not_expose_python_configuration(
    router_class: type[APIRouter],
) -> None:
    router = router_class()

    @router.get('/value')
    async def endpoint(value: int) -> int:
        return value

    result = TestClient(application(router)).get('/value', params={'value': '1' * 5000})
    assert result.status_code == 422
    assert 'sys.set_int_max_str_digits' not in result.text


@pytest.mark.parametrize('router_class,model', [(APIRouter, PydItem), (MsgspecRouter, Item)])
@pytest.mark.parametrize('response_class', [JSONResponse, PlainTextResponse])
def test_typed_custom_response_class(
    router_class: type[APIRouter], model: Any, response_class: type[Response]
) -> None:
    router = router_class()
    options = (
        {'response_model': model} if response_class is JSONResponse else {'response_model': str}
    )

    @router.get('/value', response_class=response_class, **options)
    async def endpoint(response: Response) -> Any:
        response.status_code = 202
        response.headers['X-Endpoint'] = 'kept'
        return {'value': 3, 'private': 'hidden'} if response_class is JSONResponse else 'ready'

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 202 and result.headers['X-Endpoint'] == 'kept'
    if response_class is JSONResponse:
        assert result.json() == {'value': 3}
    else:
        assert result.text == 'ready' and result.headers['Content-Type'].startswith('text/plain')


def test_typed_custom_response_still_validates() -> None:
    router = MsgspecRouter()

    @router.get('/value', response_class=JSONResponse, response_model=Item)
    async def endpoint() -> Any:
        return {'value': 'wrong'}

    with pytest.raises(ResponseValidationError):
        TestClient(application(router)).get('/value')


@pytest.mark.parametrize('router_class,model', [(APIRouter, PydItem), (MsgspecRouter, Item)])
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('sse', [False, True])
@pytest.mark.parametrize('with_body', [False, True])
def test_generator_endpoints_stream_validated_items(
    router_class: type[APIRouter], model: Any, asynchronous: bool, sse: bool, with_body: bool
) -> None:
    router = router_class()
    closed = []
    if asynchronous:

        async def endpoint(body: model) -> AsyncIterator[model]:
            try:
                yield body
                yield {'value': 2, 'private': 'hidden'}
            finally:
                closed.append(True)
    else:

        def endpoint(body: model) -> Iterator[model]:
            try:
                yield body
                yield {'value': 2, 'private': 'hidden'}
            finally:
                closed.append(True)

    if not with_body:
        original = endpoint
        if asynchronous:

            async def endpoint() -> AsyncIterator[model]:
                async for item in original(model(value=1)):
                    yield item
        else:

            def endpoint() -> Iterator[model]:
                yield from original(model(value=1))

    router.add_api_route(
        '/value',
        endpoint,
        methods=['POST'],
        **({'response_class': EventSourceResponse} if sse else {}),
    )
    app = application(router)
    result = TestClient(app).post('/value', **({'json': {'value': 1}} if with_body else {}))
    assert result.status_code == 200
    content = result.text
    rows = [
        json.loads(line[6:] if sse else line)
        for line in content.splitlines()
        if line and not line.startswith(':')
    ]
    assert rows == [{'value': 1}, {'value': 2}] and closed == [True]
    assert result.headers['Content-Type'].startswith(
        'text/event-stream' if sse else 'application/jsonl'
    )
    document = app.openapi()
    media = document['paths']['/value']['post']['responses']['200']['content']
    schema = media['text/event-stream' if sse else 'application/jsonl']['itemSchema']
    if sse:
        schema = schema['properties']['data']['contentSchema']
    assert '$ref' in schema
    assert schema['$ref'].rsplit('/', 1)[-1] in document['components']['schemas']


@pytest.mark.parametrize('sse', [False, True])
def test_streaming_body_validation_precedes_headers_after_dependencies(sse: bool) -> None:
    calls = []

    def auth() -> None:
        calls.append('auth')
        raise HTTPException(401)

    router = MsgspecRouter()
    options = {'response_class': EventSourceResponse} if sse else {}

    @router.post('/value', **options)
    async def endpoint(body: Item, limit: int = 1) -> AsyncIterator[Item]:
        calls.append('endpoint')
        yield body

    app = application(router)
    client = TestClient(app)
    assert client.post('/value', json={'value': 'wrong'}).status_code == 422
    assert client.post('/value?limit=wrong', json={'value': 'wrong'}).json()['detail'][0][
        'loc'
    ] == ['query', 'limit']
    assert calls == []
    secured = MsgspecRouter(dependencies=[Depends(auth)])
    secured.add_api_route('/value', endpoint, methods=['POST'], **options)
    assert (
        TestClient(application(secured)).post('/value', json={'value': 'wrong'}).status_code == 401
    )
    assert calls == ['auth']


def test_sse_event_metadata_and_struct_payload() -> None:
    router = MsgspecRouter()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(
            raw_data=msgspec.json.encode(Item(1)).decode('utf-8'),
            id='event-1',
            event='updated',
            retry=123,
        )
        yield ServerSentEvent(raw_data='raw', comment='kept')

    response = TestClient(application(router)).get('/value')
    assert response.status_code == 200
    assert (
        'id: event-1\n' in response.text
        and 'event: updated\n' in response.text
        and 'retry: 123\n' in response.text
    )
    payloads = [
        line.removeprefix('data: ')
        for line in response.text.splitlines()
        if line.startswith('data: ')
    ]
    assert json.loads(payloads[0]) == {'value': 1}
    assert payloads[1:] == ['raw'] and ': kept\n' in response.text


def test_raw_streaming_remains_raw_with_struct_body() -> None:
    router = MsgspecRouter()

    @router.post('/value', response_class=StreamingResponse)
    async def endpoint(body: Item) -> AsyncIterator[bytes]:
        yield str(body.value).encode()
        yield b' tail'

    response = TestClient(application(router)).post('/value', json={'value': 1})
    assert response.content == b'1 tail'


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_nested_backtracking_pattern_has_a_bounded_request(router_class: type[APIRouter]) -> None:
    import subprocess
    import sys

    code = """
import sys
from typing import Annotated
from fastapi import APIRouter,FastAPI,Query
from fastapi.testclient import TestClient
from msgspec_fastapi import MsgspecRouter
r=MsgspecRouter() if sys.argv[1]=='msgspec' else APIRouter()
@r.get('/value')
async def endpoint(value:Annotated[str,Query(pattern='(a+)+$')])->str:return value
a=FastAPI();a.include_router(r)
assert TestClient(a).get('/value',params={'value':'a'*40+'!'}).status_code==422
"""
    subprocess.run(
        [sys.executable, '-c', code, 'msgspec' if router_class is MsgspecRouter else 'pydantic'],
        check=True,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize('sse', [False, True])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_disconnect_closes_stream_generator(sse: bool, asynchronous: bool) -> None:
    import asyncio

    import anyio

    closed = []
    router = MsgspecRouter()
    if asynchronous:

        async def endpoint() -> AsyncIterator[int]:
            try:
                yield 1
                await anyio.sleep_forever()
            finally:
                closed.append(True)
    else:

        def endpoint() -> Iterator[int]:
            try:
                while True:
                    yield 1
            finally:
                closed.append(True)

    router.add_api_route(
        '/value', endpoint, **({'response_class': EventSourceResponse} if sse else {})
    )
    app = application(router)

    async def run() -> None:
        first_body = asyncio.Event()

        async def receive() -> dict[str, Any]:
            await first_body.wait()
            return {'type': 'http.disconnect'}

        async def send(message: dict[str, Any]) -> None:
            if message['type'] == 'http.response.body' and message.get('body'):
                first_body.set()

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'http',
            'path': '/value',
            'raw_path': b'/value',
            'query_string': b'',
            'root_path': '',
            'headers': [],
            'client': ('test', 123),
            'server': ('test', 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=3)

    asyncio.run(run())
    assert closed == [True]


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('alternative', [Any, str, Annotated[str, Field(min_length=3)]])
@pytest.mark.parametrize('reverse', [False, True])
def test_union_patterns_accept_other_valid_branches(
    router_class: type[APIRouter], alternative: Any, reverse: bool
) -> None:
    router = router_class()
    pattern = Annotated[str, Field(pattern='^a$')]
    target = alternative | pattern if reverse else pattern | alternative

    @router.post('/value')
    async def endpoint(value: Annotated[target, Body()]) -> str:
        return value

    result = TestClient(application(router)).post('/value', json='different')
    assert result.status_code == 200 and result.json() == 'different'


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_union_pattern_constraint_on_the_whole_value_still_applies(
    router_class: type[APIRouter],
) -> None:
    router = router_class()

    @router.post('/value')
    async def endpoint(value: Annotated[Any | str, Body(pattern='^a$')]) -> str:
        return value

    result = TestClient(application(router)).post('/value', json='different')
    assert result.status_code == 422


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_bound_generator_methods(router_class: type[APIRouter], asynchronous: bool) -> None:
    class Service:
        if asynchronous:

            async def endpoint(self) -> AsyncIterator[int]:
                yield 1
        else:

            def endpoint(self) -> Iterator[int]:
                yield 1

    router = router_class()
    router.get('/value')(Service().endpoint)
    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.headers['content-type'] == 'application/jsonl'
    assert result.text == '1\n'


@pytest.mark.parametrize('subclass', [False, True])
def test_custom_json_response_supports_raw_values(subclass: bool) -> None:
    from msgspec_fastapi import MsgspecJSONResponse

    class SelectedResponse(MsgspecJSONResponse if subclass else JSONResponse):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.headers['x-selected'] = 'yes'

    class Payload(msgspec.Struct):
        raw: msgspec.Raw

    router = MsgspecRouter()

    @router.get('/value', response_class=SelectedResponse)
    async def endpoint() -> Payload:
        return Payload(msgspec.Raw(b'{"a":1}'))

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200 and result.json() == {'raw': {'a': 1}}
    assert result.headers['x-selected'] == 'yes'


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('response_class', [JSONResponse, PlainTextResponse, Response])
def test_custom_response_openapi_media_matches_selected_class(
    router_class: type[APIRouter], response_class: type[Response]
) -> None:
    router = router_class()

    @router.get('/value', response_class=response_class)
    async def endpoint() -> str:
        return 'hello'

    app = application(router)
    result = TestClient(app).get('/value')
    assert result.status_code == 200
    documented = app.openapi()['paths']['/value']['get']['responses']['200'].get('content', {})
    assert set(documented) == ({response_class.media_type} if response_class.media_type else set())


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_stream_body_default_is_not_copied_by_the_media_check(
    router_class: type[APIRouter],
) -> None:
    from dataclasses import dataclass

    calls = []

    @dataclass
    class Payload:
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            calls.append(True)
            return Payload(self.value + len(calls))

    router = router_class()

    @router.post('/value')
    async def endpoint(body: Annotated[Payload, Body()] = Payload(10)) -> AsyncIterator[int]:
        yield body.value

    app = application(router)
    calls.clear()
    result = TestClient(app).post('/value')
    assert result.status_code == 200 and result.text == '13\n'
    assert len(calls) == 2


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_union_patterns_accept_a_valid_literal_alternative(router_class: type[APIRouter]) -> None:
    from typing import Literal

    router = router_class()
    target = Annotated[str, Field(pattern='^a$')] | Literal['b']

    @router.post('/value')
    async def endpoint(value: Annotated[target, Body()]) -> str:
        return value

    client = TestClient(application(router))
    assert client.post('/value', json='b').json() == 'b'
    assert client.post('/value', json='other').status_code == 422


@pytest.mark.parametrize('router_class,model', [(APIRouter, PydItem), (MsgspecRouter, Item)])
def test_explicit_sse_event_retains_native_item_validation_bypass(
    router_class: type[APIRouter], model: Any
) -> None:
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[model]:
        yield ServerSentEvent(data={'value': 'invalid', 'extra': 'kept'})

    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router))
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert json.loads(result.text.removeprefix('data: ').strip()) == {
        'value': 'invalid',
        'extra': 'kept',
    }


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('sse', [False, True])
def test_nonfinite_stream_item_keeps_json_null_encoding(
    router_class: type[APIRouter], sse: bool
) -> None:
    router = router_class()

    async def endpoint() -> AsyncIterator[float]:
        yield float('nan')

    router.add_api_route(
        '/value', endpoint, **({'response_class': EventSourceResponse} if sse else {})
    )
    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.text == ('data: null\n\n' if sse else 'null\n')


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), None])
def test_explicit_sse_event_keeps_nonfinite_payload_distinct_from_absent_data(
    router_class: type[APIRouter], value: Any
) -> None:
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=value, id='event-1')

    if router_class is MsgspecRouter and value is not None:
        assert_sse_rejected(application(router))
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    if value is None:
        assert result.text == 'id: event-1\n\n'
    else:
        # Explicit events retain native encoding across supported FastAPI versions.
        encodings = {'null', json.dumps(value)}
        assert result.text in {f'data: {data}\nid: event-1\n\n' for data in encodings}


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_explicit_sse_raw_data_has_precedence(router_class: type[APIRouter]) -> None:
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(raw_data='kept', id='event-1').model_copy(
            update={'data': float('nan')}
        )

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.text == 'data: kept\nid: event-1\n\n'


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('kind', ['alias', 'custom_model', 'custom_object'])
def test_explicit_sse_payload_retains_native_json_serializer(
    router_class: type[APIRouter], kind: str
) -> None:
    calls = []

    class Aliased(BaseModel):
        internal: int = Field(alias='external')

    class CustomModel(BaseModel):
        value: int

        def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
            calls.append('serialized')
            return '{"wire":"custom"}'

    class CustomObject:
        def model_dump_json(self) -> str:
            calls.append('serialized')
            return '{"wire":"custom"}'

    payload = {
        'alias': Aliased(external=7),
        'custom_model': CustomModel(value=7),
        'custom_object': CustomObject(),
    }[kind]
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=payload, id='event-1')

    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router))
        assert calls == []
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    data = '{"internal":7}' if kind == 'alias' else '{"wire":"custom"}'
    assert result.text == f'data: {data}\nid: event-1\n\n'
    assert calls == ([] if kind == 'alias' else ['serialized'])


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('kind', ['property', 'dynamic', 'struct_property'])
def test_explicit_sse_serializer_lookups_are_not_repeated(
    router_class: type[APIRouter], kind: str
) -> None:
    calls = []

    def serializer() -> Any:
        calls.append('lookup')

        def dump() -> str:
            calls.append('serialized')
            return json.dumps({'lookups': calls.count('lookup')}, separators=(',', ':'))

        return dump

    class PropertyData:
        @property
        def model_dump_json(self) -> Any:
            return serializer()

    class DynamicData:
        def __getattr__(self, name: str) -> Any:
            if name == 'model_dump_json':
                return serializer()
            raise AttributeError(name)

    class StructData(msgspec.Struct):
        value: int

        @property
        def model_dump_json(self) -> Any:
            return serializer()

    data = {'property': PropertyData(), 'dynamic': DynamicData(), 'struct_property': StructData(7)}[
        kind
    ]
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=data, id='event-1')

    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router))
        assert calls == []
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.text == 'data: {"lookups":2}\nid: event-1\n\n'
    assert calls == ['lookup', 'lookup', 'serialized']


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('data', [b'ab', Decimal('1.25')])
def test_explicit_sse_payload_keeps_native_scalar_encoding(
    router_class: type[APIRouter], data: Any
) -> None:
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=data)

    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router))
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.text == ('data: "ab"\n\n' if isinstance(data, bytes) else 'data: 1.25\n\n')


def test_explicit_sse_msgspec_raw_null_remains_present() -> None:
    router = MsgspecRouter()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(
            raw_data=msgspec.json.encode(msgspec.Raw(b'null')).decode('utf-8'),
            id='raw',
        )

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200 and result.text == 'data: null\nid: raw\n\n'


@pytest.mark.parametrize('router_class,model', [(APIRouter, PydItem), (MsgspecRouter, Item)])
@pytest.mark.parametrize('shape', ['list', 'dict'])
def test_explicit_sse_nested_payloads(
    router_class: type[APIRouter], model: Any, shape: str
) -> None:
    router = router_class()
    raw = msgspec.Raw(b'null') if router_class is MsgspecRouter else None
    data = [model(value=1), raw] if shape == 'list' else {'item': model(value=1), 'raw': raw}

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        if router_class is MsgspecRouter:
            yield ServerSentEvent(raw_data=msgspec.json.encode(data).decode('utf-8'))
        else:
            yield ServerSentEvent(data=data)

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    expected = [{'value': 1}, None] if shape == 'list' else {'item': {'value': 1}, 'raw': None}
    assert json.loads(result.text.removeprefix('data: ').strip()) == expected


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('callback', ['model_copy', '__copy__'])
def test_explicit_sse_does_not_invoke_event_copy_callbacks(
    router_class: type[APIRouter], callback: str
) -> None:
    calls = []

    class Payload(msgspec.Struct):
        value: int

        def model_dump_json(self) -> str:
            return '{"value":7}'

    class Event(ServerSentEvent):
        if callback == 'model_copy':

            def model_copy(self, **kwargs: Any) -> Any:
                calls.append('model_copy')
                result = super().model_copy(**kwargs)
                result.id = 'copied'
                return result
        else:

            def __copy__(self) -> Any:
                calls.append('__copy__')
                result = super().__copy__()
                result.id = 'copied'
                return result

    original = Event(data=Payload(7), id='original', event='updated', retry=5, comment='keep')
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield original

    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router), 'ServerSentEvent subclasses are not supported')
        assert calls == []
        assert original.id == 'original' and original.raw_data is None
        assert original.data.value == 7
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.text == ': keep\nevent: updated\ndata: {"value":7}\nid: original\nretry: 5\n\n'
    assert calls == []
    assert original.id == 'original' and original.raw_data is None
    assert original.data.value == 7


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
def test_native_sse_payload_does_not_invoke_numeric_hooks(
    router_class: type[APIRouter],
) -> None:
    calls = []

    class Value:
        def __init__(self) -> None:
            self.value = 7

        def __int__(self) -> int:
            calls.append('int')
            return 7

        def __float__(self) -> float:
            calls.append('float')
            return 7.0

        def __bool__(self) -> bool:
            calls.append('bool')
            return True

        def __str__(self) -> str:
            calls.append('str')
            return 'changed'

    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=[Value()])

    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router))
        assert calls == []
        return

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert json.loads(result.text.removeprefix('data: ').strip()) == [{'value': 7}]
    assert calls == []


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('shape', ['direct', 'list', 'dict'])
def test_native_sse_payload_does_not_invoke_metaclass_comparisons(
    router_class: type[APIRouter], shape: str
) -> None:
    calls = []

    class Meta(type):
        def __eq__(cls, other: Any) -> bool:
            calls.append('comparison')
            return False

        __hash__ = type.__hash__

    class Value(metaclass=Meta):
        def model_dump_json(self) -> str:
            return json.dumps({'comparisons': len(calls)}, separators=(',', ':'))

    value = Value()
    data = {'direct': value, 'list': [value], 'dict': {'item': value}}[shape]
    router = router_class()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=data)

    app = application(router)
    calls.clear()
    if router_class is MsgspecRouter:
        assert_sse_rejected(application(router))
        assert calls == []
        return

    result = TestClient(app).get('/value')
    assert result.status_code == 200
    expected = {'direct': {'comparisons': 0}, 'list': [{}], 'dict': {'item': {}}}[shape]
    assert json.loads(result.text.removeprefix('data: ').strip()) == expected
    assert calls == []


@pytest.mark.parametrize('where', ['query', 'body', 'nested', 'response'])
def test_compiled_python_patterns_are_rejected(where: str) -> None:
    pattern = re.compile('(a+)+$', re.I)
    target = Annotated[str, Field(pattern=pattern)]
    annotation = {
        'query': Annotated[str, Query(pattern=pattern)],
        'body': Annotated[str, Body(pattern=pattern)],
        'nested': list[target],
        'response': str,
    }[where]

    async def endpoint(value: annotation) -> Any:
        return value

    with pytest.raises(TypeError, match='compiled Python patterns are not supported'):
        MsgspecRouter().add_api_route(
            '/value',
            endpoint,
            methods=['POST'],
            **({'response_model': target} if where == 'response' else {}),
        )


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize(
    'pattern,value',
    [
        (r'^\w+$', 'é١𐐀'),
        (r'^\w+$', 'a\u0301'),
        (r'^\d+$', '١٢٣'),
        (r'^\p{Greek}+$', 'αβγ'),
    ],
)
def test_string_patterns_keep_unicode_classes(
    router_class: type[APIRouter],
    pattern: str,
    value: str,
) -> None:
    router = router_class()

    @router.get('/value')
    async def endpoint(value: Annotated[str, Query(pattern=pattern)]) -> str:
        return value

    client = TestClient(application(router))
    assert client.get('/value', params={'value': value}).json() == value
    assert client.get('/value', params={'value': '!'}).status_code == 422


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('mode', ['ordinary', 'jsonl', 'sse'])
def test_patterned_msgspec_requests_do_not_run_pydantic_validation(
    router_class: type[APIRouter],
    mode: str,
) -> None:
    import asyncio
    import sys

    import httpx

    pattern = Annotated[str, Field(pattern='^a+$')]
    router = router_class()

    async def endpoint(
        value: list[pattern],
        query: Annotated[str, Query(pattern='^a+$')],
    ) -> list[pattern]:
        return value

    async def stream_endpoint(
        value: list[pattern],
        query: Annotated[str, Query(pattern='^a+$')],
    ) -> AsyncIterator[list[pattern]]:
        yield value

    router.add_api_route(
        '/value',
        endpoint if mode == 'ordinary' else stream_endpoint,
        methods=['POST'],
        **({'response_class': EventSourceResponse} if mode == 'sse' else {}),
    )
    app = application(router)
    observed = []

    def profile(frame: Any, event: str, arg: Any) -> None:
        if (
            event == 'c_call'
            and getattr(arg, '__name__', '') == 'validate_python'
            and isinstance(getattr(arg, '__self__', None), SchemaValidator)
        ):
            observed.append(arg)

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://local',
        ) as client:
            for body, query, status in [
                (['aa'], 'aa', 200),
                (['bad'], 'aa', 422),
                (['aa'], 'bad', 422),
            ]:
                observed.clear()
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    response = await client.post('/value', json=body, params={'query': query})
                finally:
                    sys.setprofile(previous)
                assert response.status_code == status
                if status == 200:
                    if mode == 'ordinary':
                        assert response.json() == ['aa']
                    else:
                        assert response.text == (
                            'data: ["aa"]\n\n' if mode == 'sse' else '["aa"]\n'
                        )
                if router_class is MsgspecRouter:
                    assert observed == []
                else:
                    assert observed, 'The plain FastAPI control must exercise the profiler'

    asyncio.run(check())


@pytest.mark.parametrize('shape', ['list', 'tuple', 'nested'])
@pytest.mark.parametrize('embedded', [False, True])
def test_strict_body_pattern_errors_keep_every_original_position(
    shape: str, embedded: bool
) -> None:
    pattern = Annotated[str, Field(pattern='^a+$')]
    annotation = {
        'list': list[pattern],
        'tuple': tuple[pattern, ...],
        'nested': list[list[pattern]],
    }[shape]
    if embedded:
        annotation = Annotated[annotation, Body(embed=True, alias='values')]
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(value: annotation) -> Any:
        return value

    test_client = TestClient(application(router))
    payload = ['bad', 'aa', 'a\n', 'bad']
    if shape == 'nested':
        payload = [payload]
    response = test_client.post('/value', json={'values': payload} if embedded else payload)
    assert response.status_code == 422
    prefix = ['body'] + (['values'] if embedded else []) + ([0] if shape == 'nested' else [])
    assert [error['loc'] for error in response.json()['detail']] == [
        prefix + [index] for index in (0, 2, 3)
    ]
    assert all(
        error['type'] == 'string_pattern_mismatch' and error['ctx'] == {'pattern': '^a+$'}
        for error in response.json()['detail']
    )
    valid = [['a', 'aa']] if shape == 'nested' else ['a', 'aa']
    assert test_client.post('/value', json={'values': valid} if embedded else valid).json() == valid


@pytest.mark.parametrize('mode', ['response', 'jsonl', 'sse'])
def test_stdlib_path_and_ip_values_keep_explicit_encoding(mode: str) -> None:
    payload = {
        'path': Path('/tmp/a'),
        'ip': ipaddress.ip_address('::1'),
        'interface': ipaddress.ip_interface('127.0.0.1/8'),
        'network': ipaddress.ip_network('127.0.0.0/8'),
    }
    router = MsgspecRouter()
    if mode == 'response':

        async def endpoint():  # noqa: ANN202 - Exercise the untyped response encoder.
            return payload
    else:

        async def endpoint() -> AsyncIterator[Any]:
            yield payload

    router.add_api_route(
        '/value',
        endpoint,
        response_model=None,
        **({'response_class': EventSourceResponse} if mode == 'sse' else {}),
    )
    response = TestClient(application(router)).get('/value')
    assert response.status_code == 200
    content = response.text.removeprefix('data: ').strip() if mode == 'sse' else response.text
    assert json.loads(content) == {
        'path': str(Path('/tmp/a')),
        'ip': '::1',
        'interface': '127.0.0.1/8',
        'network': '127.0.0.0/8',
    }
    if mode != 'response':
        assert response.text.endswith('\n\n' if mode == 'sse' else '\n')


@pytest.mark.parametrize('kind', ['url', 'secret'])
@pytest.mark.parametrize('nested', [False, True])
def test_untyped_pydantic_scalars_are_rejected_without_extraction(
    kind: str, nested: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    scalar_class = SecretStr if kind == 'secret' else HttpUrl
    original = scalar_class.__str__

    def stringify(value: Any) -> str:
        calls.append('str')
        return original(value)

    monkeypatch.setattr(scalar_class, '__str__', stringify)
    value = (
        SecretStr('must-not-leak')
        if kind == 'secret'
        else HttpUrl('https://example.com/private-credential')
    )
    router = MsgspecRouter()

    @router.get('/value')
    async def endpoint():  # noqa: ANN202 - Exercise the untyped response encoder.
        return {'items': [value]} if nested else value

    calls.clear()
    response = TestClient(application(router), raise_server_exceptions=False).get('/value')
    assert response.status_code == 500
    assert calls == []
    assert 'must-not-leak' not in response.text and 'private-credential' not in response.text


def test_explicit_raw_sse_keeps_metadata_without_model_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    value = ServerSentEvent(
        raw_data='{"value":7}', id='original', event='updated', retry=5, comment='keep'
    )

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        calls.append('model operation')
        raise RuntimeError('Unexpected Pydantic model operation')

    for name in ('model_dump', 'model_dump_json', 'model_copy', '__copy__'):
        monkeypatch.setattr(ServerSentEvent, name, unexpected)
    router = MsgspecRouter()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[ServerSentEvent]:
        yield value

    result = TestClient(application(router)).get('/value')
    assert result.status_code == 200
    assert result.text == ': keep\nevent: updated\ndata: {"value":7}\nid: original\nretry: 5\n\n'
    assert calls == []
    assert value.id == 'original' and value.raw_data == '{"value":7}' and value.data is None
