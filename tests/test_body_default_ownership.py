"""Default data is checked before framework or route copies."""

import dataclasses
import sys
import threading
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest
from fastapi import Body, FastAPI
from fastapi.sse import EventSourceResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from pydantic.dataclasses import dataclass as validated_dataclass

from msgspec_fastapi import MsgspecRouter


class External(BaseModel):
    value: int


@validated_dataclass(config=ConfigDict(validate_assignment=True), slots=True)
class ExternalSlots:
    value: int


def application(default: Any, mode: str, embedded: bool) -> FastAPI:
    annotation = Annotated[Any, Body(embed=embedded, alias='payload')]
    router = MsgspecRouter()
    if mode == 'ordinary':

        async def endpoint(value: annotation = default) -> int:
            return value.value if hasattr(value, 'value') else len(value['items'])
    else:

        async def endpoint(value: annotation = default) -> AsyncIterator[int]:
            yield value.value if hasattr(value, 'value') else len(value['items'])

    options = {'response_class': EventSourceResponse} if mode == 'sse' else {}
    router.add_api_route('/items', endpoint, methods=['POST'], **options)
    app = FastAPI()
    app.include_router(router)
    return app


def record_model_calls(frame: Any, event: str, arg: Any, calls: list[str]) -> None:
    if event == 'call' and (
        frame.f_code.co_filename.endswith('/pydantic/main.py')
        and frame.f_code.co_name in ('__deepcopy__', 'model_dump', 'model_dump_json')
        or frame.f_code.co_name in ('_dataclass_getstate', '_dataclass_setstate')
    ):
        calls.append(frame.f_code.co_name)


@pytest.mark.parametrize('mode', ['ordinary', 'jsonl', 'sse'])
@pytest.mark.parametrize('embedded', [False, True])
@pytest.mark.parametrize('model', [External, ExternalSlots])
@pytest.mark.parametrize('nested', [False, True])
def test_branded_defaults_are_rejected_before_model_copy(
    mode: str, embedded: bool, model: Any, nested: bool
) -> None:
    value = model(value=1)
    default = {'items': [value]} if nested else value
    calls = []
    previous = sys.getprofile()
    previous_thread = threading.getprofile()
    try:

        def hook(frame: Any, event: str, arg: Any) -> None:
            record_model_calls(frame, event, arg, calls)

        sys.setprofile(hook)
        threading.setprofile(hook)
        with pytest.raises(TypeError):
            application(default, mode, embedded)
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_thread)
    assert calls == []


@pytest.mark.parametrize('mode', ['ordinary', 'jsonl', 'sse'])
@pytest.mark.parametrize('embedded', [False, True])
@pytest.mark.parametrize('model', [External, ExternalSlots])
def test_post_registration_default_mutation_is_checked_before_copy(
    mode: str, embedded: bool, model: Any
) -> None:
    default: Any = {'items': []}
    app = application(default, mode, embedded)
    default['items'].append(model(value=1))
    calls = []
    previous = sys.getprofile()
    previous_thread = threading.getprofile()
    try:

        def hook(frame: Any, event: str, arg: Any) -> None:
            record_model_calls(frame, event, arg, calls)

        sys.setprofile(hook)
        threading.setprofile(hook)
        with pytest.raises(TypeError):
            TestClient(app).post('/items', json={} if embedded else None)
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_thread)
    assert calls == []


@pytest.mark.parametrize('mode', ['ordinary', 'jsonl', 'sse'])
@pytest.mark.parametrize('embedded', [False, True])
def test_native_default_copy_counts_are_preserved(mode: str, embedded: bool) -> None:
    calls = []

    @dataclasses.dataclass
    class Payload:
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            calls.append('copy')
            return Payload(self.value + len(calls))

    app = application(Payload(10), mode, embedded)
    calls.clear()
    result = TestClient(app).post('/items', json={} if embedded else None)
    assert result.status_code == 200
    if mode == 'ordinary':
        assert result.json() == 11 and calls == ['copy']
    else:
        assert result.text == ('data: 13\n\n' if mode == 'sse' else '13\n')
        assert calls == ['copy', 'copy']


def test_dataclass_container_defaults_cannot_hide_branded_values() -> None:
    @dataclasses.dataclass
    class Mapping(dict):
        pass

    value = Mapping()
    value['items'] = [ExternalSlots(1)]
    with pytest.raises(TypeError):
        application(value, 'ordinary', False)


@pytest.mark.parametrize('slotted', [False, True])
@pytest.mark.parametrize('mode', ['ordinary', 'jsonl', 'sse'])
def test_dataclass_instance_state_is_checked_beyond_json_fields(slotted: bool, mode: str) -> None:
    @dataclasses.dataclass
    class Payload:
        value: int

    class SlottedPayload(Payload):
        __slots__ = ('extra',)

    value = SlottedPayload(1) if slotted else Payload(1)
    value.extra = External(value=1)
    calls = []
    previous = sys.getprofile()
    try:

        def hook(frame: Any, event: str, arg: Any) -> None:
            record_model_calls(frame, event, arg, calls)

        sys.setprofile(hook)
        with pytest.raises(TypeError):
            application(value, mode, False)
    finally:
        sys.setprofile(previous)
    assert calls == []
