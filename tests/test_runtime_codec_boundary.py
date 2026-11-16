"""Observe real request validation and encoding, including a native positive control."""

import asyncio
import sys
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Any

import httpx
import msgspec
from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter


class Input(msgspec.Struct):
    value: int


class NativeInput(BaseModel):
    value: int


async def observed_request(app: FastAPI, payload: Any) -> tuple[httpx.Response, list[str]]:
    calls = []

    def observe(frame: Any, event: str, arg: Any) -> None:
        if event == 'c_call':
            owner = getattr(arg, '__self__', None)
            if type(owner).__module__.startswith('pydantic_core'):
                name = getattr(arg, '__name__', '')
                if name in (
                    'validate_python',
                    'validate_json',
                    'validate_strings',
                    'to_python',
                    'to_json',
                ):
                    calls.append(name)
        elif (
            event == 'call'
            and frame.f_code.co_name == 'jsonable_encoder'
            and frame.f_code.co_filename.endswith('/fastapi/encoders.py')
        ):
            calls.append('jsonable_encoder')

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test'
    ) as client:
        previous = sys.getprofile()
        try:
            sys.setprofile(observe)
            response = await client.post('/items?q=2', json=payload)
        finally:
            sys.setprofile(previous)
    return response, calls


def test_native_control_exercises_validation_and_serialization_observer() -> None:
    router = APIRouter()

    @router.post('/items')
    async def endpoint(value: NativeInput) -> NativeInput:
        return value

    app = FastAPI()
    app.include_router(router)
    response, calls = asyncio.run(observed_request(app, {'value': 7}))
    assert response.status_code == 200 and response.json() == {'value': 7}
    assert 'validate_python' in calls
    assert any(name in calls for name in ('to_python', 'to_json'))


def test_msgspec_request_and_stdlib_return_use_no_external_model_codec() -> None:
    router = MsgspecRouter()

    @router.post('/items', response_model=None)
    async def endpoint(value: Input, q: Annotated[int, Query(gt=0)]) -> dict[str, Any]:
        return {
            'value': value.value,
            'q': q,
            'path': Path('file.txt'),
            'ip': IPv4Address('127.0.0.1'),
        }

    app = FastAPI()
    app.include_router(router)
    response, calls = asyncio.run(observed_request(app, {'value': 7}))
    assert response.status_code == 200
    assert response.json() == {'value': 7, 'q': 2, 'path': 'file.txt', 'ip': '127.0.0.1'}
    assert calls == []
    response, calls = asyncio.run(observed_request(app, {'value': 'bad'}))
    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['body', 'value']
    assert calls == []
