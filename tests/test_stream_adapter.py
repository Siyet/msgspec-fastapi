"""Additional native-field serialization and Raw safety regression controls."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import msgspec
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.sse import EventSourceResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter, install_openapi


class Payload(msgspec.Struct):
    raw: msgspec.Raw


class Item(msgspec.Struct):
    value: int


class PydItem(BaseModel):
    value: int


def client(router: APIRouter) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if isinstance(router, MsgspecRouter):
        install_openapi(app)
    return TestClient(app)


@pytest.mark.parametrize('sse', [False, True])
def test_untyped_msgspec_stream_keeps_struct_serialization(sse: bool) -> None:
    router = MsgspecRouter()

    @router.get(
        '/items', response_model=None, **({'response_class': EventSourceResponse} if sse else {})
    )
    async def endpoint() -> AsyncIterator[Any]:
        yield Item(7)

    result = client(router).get('/items')
    assert result.status_code == 200
    assert result.text == ('data: {"value":7}\n\n' if sse else '{"value":7}\n')


@pytest.mark.parametrize('trusted', [False, True])
def test_pretty_raw_is_compacted_to_one_jsonl_item(trusted: bool) -> None:
    router = MsgspecRouter()
    pretty = msgspec.Raw(b'{\n "x": 1,\n "values": [1, 2]\n}')

    async def endpoint() -> AsyncIterator[Any]:
        yield Payload(pretty) if trusted else pretty

    router.add_api_route('/items', endpoint, response_model=Payload if trusted else None)
    result = client(router).get('/items')
    assert result.status_code == 200
    assert result.text == (
        '{"raw":{"x":1,"values":[1,2]}}\n' if trusted else '{"x":1,"values":[1,2]}\n'
    )
    assert len(result.text.splitlines()) == 1


@pytest.mark.parametrize('trusted', [False, True])
@pytest.mark.parametrize(
    'raw,error',
    [
        (b'{', msgspec.DecodeError),
        (b'1 2', msgspec.DecodeError),
        (b'"\xff"', UnicodeDecodeError),
        (b'"\\ud800"', msgspec.DecodeError),
        (b'"\\udfff"', msgspec.DecodeError),
        (b'1e999', msgspec.ValidationError),
        (b'9' * 5000, msgspec.ValidationError),
        (b'{"v":"\\ud800"}', msgspec.DecodeError),
        (b'{"v":1e999}', msgspec.ValidationError),
        (b'{"v":' + b'9' * 5000 + b'}', msgspec.ValidationError),
    ],
)
def test_malformed_raw_does_not_bypass_stream_json_validation(
    trusted: bool, raw: bytes, error: type[Exception]
) -> None:
    router = MsgspecRouter()

    async def endpoint() -> AsyncIterator[Any]:
        yield Payload(msgspec.Raw(raw)) if trusted else msgspec.Raw(raw)

    router.add_api_route('/items', endpoint, response_model=Payload if trusted else None)
    with pytest.raises(error):
        client(router).get('/items')


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('sync', [False, True])
def test_pydantic_jsonl_still_uses_native_model_projection(
    router_class: type[APIRouter], sync: bool
) -> None:
    router = router_class()
    if sync:

        def endpoint() -> Iterator[PydItem]:
            yield {'value': 7, 'private': 'hidden'}
    else:

        async def endpoint() -> AsyncIterator[PydItem]:
            yield {'value': 7, 'private': 'hidden'}

    if router_class is MsgspecRouter:
        with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
            router.get('/items')(endpoint)
        return

    router.get('/items')(endpoint)
    result = client(router).get('/items')
    assert result.status_code == 200 and result.headers['content-type'] == 'application/jsonl'
    assert result.text == '{"value":7}\n'
