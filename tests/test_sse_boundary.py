"""Only native SSE metadata objects bypass the msgspec item codec."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient

from msgspec_fastapi import MsgspecRouter, install_openapi


def test_runtime_event_check_avoids_unrelated_metaclass_hooks() -> None:
    calls = []

    class Hostile(type):
        __hash__ = type.__hash__

        def __getattribute__(cls, name: str) -> Any:
            if name in ('__mro__', '__bases__', '__dict__'):
                calls.append(name)
                raise RuntimeError(name)
            return type.__getattribute__(cls, name)

        def __eq__(cls, other: Any) -> bool:
            calls.append('equality')
            raise RuntimeError('Metaclass equality executed')

    class Payload(metaclass=Hostile):
        pass

    value = Payload()
    router = MsgspecRouter()

    @router.get('/value', response_class=EventSourceResponse)
    async def endpoint() -> AsyncIterator[Any]:
        yield value

    app = FastAPI()
    app.include_router(router)
    with pytest.raises((TypeError, ExceptionGroup)):
        TestClient(app).get('/value')
    assert calls == []


def test_event_inheritance_check_avoids_pydantic_metaclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    metaclass = type(ServerSentEvent)
    original = metaclass.__subclasscheck__

    class Event(ServerSentEvent):
        pass

    def observed(cls: type, candidate: type) -> bool:
        if cls is ServerSentEvent:
            calls.append('subclasscheck')
        return original(cls, candidate)

    monkeypatch.setattr(metaclass, '__subclasscheck__', observed)
    assert issubclass(Event, ServerSentEvent) and calls == ['subclasscheck']
    calls.clear()

    async def endpoint() -> AsyncIterator[Event]:
        yield Event()

    with pytest.raises(TypeError, match='subclasses'):
        MsgspecRouter().get('/value', response_class=EventSourceResponse)(endpoint)
    assert calls == []


@pytest.mark.parametrize('router_class', [APIRouter, MsgspecRouter])
@pytest.mark.parametrize('annotation', ['missing', 'base', 'any'])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_event_subclass_is_rejected_before_field_getters(
    router_class: type[APIRouter], annotation: str, asynchronous: bool
) -> None:
    reads = []
    closed = []

    class Event(ServerSentEvent):
        def __getattribute__(self, name: str) -> Any:
            if name == 'raw_data':
                reads.append(name)
                if len(reads) > 2:
                    raise RuntimeError('Additional field access')
            return super().__getattribute__(name)

    event = Event(raw_data='literal', id='kept')
    if asynchronous:

        async def endpoint():  # noqa: ANN202 - Test absent, base and Any stream annotations.
            try:
                yield event
            finally:
                closed.append(True)
    else:

        def endpoint():  # noqa: ANN202 - Test absent, base and Any stream annotations.
            try:
                yield event
            finally:
                closed.append(True)

    if annotation != 'missing':
        item = ServerSentEvent if annotation == 'base' else Any
        endpoint.__annotations__['return'] = AsyncIterator[item] if asynchronous else Iterator[item]
    router = router_class()
    router.get('/value', response_class=EventSourceResponse)(endpoint)
    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    reads.clear()
    if router_class is APIRouter:
        response = TestClient(app).get('/value')
        assert response.content == b'data: literal\nid: kept\n\n'
        assert len(reads) == 2
    else:
        with pytest.raises((TypeError, ExceptionGroup)) as caught:
            TestClient(app).get('/value')

        def leaves(error: BaseException) -> list[BaseException]:
            if isinstance(error, BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]

        errors = leaves(caught.value)
        assert len(errors) == 1 and type(errors[0]) is TypeError
        assert str(errors[0]) == 'ServerSentEvent subclasses are not supported'
        assert reads == []
    assert closed == [True]


@pytest.mark.parametrize('explicit_model', [False, True])
def test_declared_event_subclass_is_rejected_at_registration(explicit_model: bool) -> None:
    class Event(ServerSentEvent):
        pass

    async def endpoint() -> AsyncIterator[Event]:
        yield Event(raw_data='value')

    options = {'response_model': None} if explicit_model else {}
    with pytest.raises(TypeError, match='ServerSentEvent subclasses are not supported'):
        MsgspecRouter().get('/value', response_class=EventSourceResponse, **options)(endpoint)
