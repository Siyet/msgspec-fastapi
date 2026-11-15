"""The msgspec data paths must not fall back to Pydantic serialization."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import fastapi.routing
import msgspec
import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.sse import EventSourceResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter

from msgspec_fastapi import MsgspecJSONResponse, MsgspecRouter, install_openapi


def app_for(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if isinstance(router, MsgspecRouter):
        install_openapi(app)
    return app


@pytest.mark.parametrize('response_class', [MsgspecJSONResponse, JSONResponse, PlainTextResponse])
@pytest.mark.parametrize('annotation', [None, Response])
def test_untyped_custom_responses_never_serialize_models(
    response_class: type[Response], annotation: Any
) -> None:
    calls = []

    class Private(BaseModel):
        secret: str

        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append('dump')
            return {'secret': self.secret}

    value = Private(secret='must-not-be-serialized')
    router = MsgspecRouter()

    async def endpoint():  # noqa: ANN202 - Missing annotation is part of this boundary.
        return value

    if annotation is not None:
        endpoint.__annotations__['return'] = annotation
    router.get('/value', response_class=response_class)(endpoint)
    result = TestClient(app_for(router), raise_server_exceptions=False).get('/value')
    assert result.status_code == 500
    assert calls == []
    assert 'must-not-be-serialized' not in result.text


def test_untyped_selected_json_class_keeps_constructor_and_stdlib_encoding() -> None:
    class Selected(JSONResponse):
        def __init__(self, content: Any, **kwargs: Any) -> None:
            super().__init__(content, **kwargs)
            self.headers['x-selected'] = 'yes'

    router = MsgspecRouter()

    @router.get('/value', response_class=Selected, response_model=None)
    async def endpoint():  # noqa: ANN202 - Exercise the untyped custom response path.
        return {'path': Path('/tmp/value')}

    result = TestClient(app_for(router)).get('/value')
    assert result.json() == {'path': '/tmp/value'}
    assert result.headers['x-selected'] == 'yes'


def test_data_paths_work_when_pydantic_runtime_entrypoints_are_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Payload(msgspec.Struct):
        value: int

    router = MsgspecRouter()

    @router.post('/value')
    async def value(body: Payload, count: Annotated[int, Query(gt=0)] = 1) -> Payload:
        return Payload(body.value * count)

    @router.post('/jsonl')
    async def jsonl(body: Annotated[Payload, Body()]) -> AsyncIterator[Payload]:
        yield body

    @router.get('/sse', response_class=EventSourceResponse)
    async def sse() -> AsyncIterator[Payload]:
        yield Payload(7)

    native = APIRouter()

    @native.get('/value')
    async def control() -> dict[str, int]:
        return {'value': 1}

    app = app_for(router)
    control_app = app_for(native)
    assert TestClient(control_app).get('/value').json() == {'value': 1}
    assert '/jsonl' in app.openapi()['paths']

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError('A Pydantic runtime or native JSON serializer was invoked')

    for method in ('validate_python', 'validate_json', 'dump_python', 'dump_json'):
        monkeypatch.setattr(TypeAdapter, method, forbidden)
    for method in ('model_dump', 'model_dump_json'):
        monkeypatch.setattr(BaseModel, method, forbidden)
    monkeypatch.setattr(fastapi.routing, 'jsonable_encoder', forbidden)
    client = TestClient(app)
    assert client.post('/value?count=2', json={'value': 3}).json() == {'value': 6}
    invalid = client.post('/value', json={'value': 'wrong'})
    assert invalid.status_code == 422
    assert invalid.json()['detail'][0]['loc'] == ['body', 'value']
    assert client.post('/value?count=0', json={'value': 3}).status_code == 422
    assert client.post('/jsonl', json={'value': 4}).content == b'{"value":4}\n'
    assert client.get('/sse').content == b'data: {"value":7}\n\n'
    with pytest.raises(RuntimeError, match='Pydantic runtime'):
        TestClient(control_app).get('/value')


def test_dependency_json_bodies_cannot_use_native_validation() -> None:
    async def dependency(value: Annotated[int, Body()]) -> int:
        return value

    router = MsgspecRouter()

    async def endpoint(value: Annotated[int, Depends(dependency)]) -> int:
        return value

    with pytest.raises(TypeError, match='JSON body fields must use the route msgspec decoder'):
        router.post('/value')(endpoint)
        app_for(router)
