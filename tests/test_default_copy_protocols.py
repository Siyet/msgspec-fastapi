"""Body default copying excludes arbitrary state reconstruction protocols."""

import dataclasses
from typing import Annotated, Any

import msgspec
import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter


def register(default: Any) -> FastAPI:
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(value: Annotated[Any, Body()] = default) -> int:
        return value.value

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.parametrize(
    'method',
    [
        '__getstate__',
        '__setstate__',
        '__reduce__',
        '__reduce_ex__',
        '__getnewargs__',
        '__getnewargs_ex__',
    ],
)
def test_custom_state_protocol_is_rejected_without_invocation(method: str) -> None:
    calls = []

    @dataclasses.dataclass
    class Record:
        value: int

    def protocol(self: Any, *args: Any) -> Any:
        calls.append(method)
        raise RuntimeError('Custom reconstruction protocol executed')

    setattr(Record, method, protocol)
    with pytest.raises(TypeError):
        register(Record(1))
    assert calls == []


def test_inherited_state_protocol_is_rejected() -> None:
    calls = []

    class State:
        def __getstate__(self) -> dict[str, int]:
            calls.append('state')
            return {'value': 1}

    @dataclasses.dataclass
    class Record(State):
        value: int

    with pytest.raises(TypeError):
        register(Record(1))
    assert calls == []


def test_standard_frozen_slots_dataclass_state_remains_supported() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class Record:
        value: int

    response = TestClient(register(Record(7))).post('/items')
    assert response.status_code == 200
    assert response.json() == 7


def test_late_state_hook_is_rejected_before_state_or_model_copy() -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            calls.append('model-copy')
            return self

    hidden = External(value=1)

    @dataclasses.dataclass
    class Record:
        value: int

    app = register(Record(1))

    def state(self: Any) -> dict[str, Any]:
        calls.append('state')
        return {'value': 1, 'hidden': hidden}

    Record.__getstate__ = state
    with pytest.raises(TypeError):
        TestClient(app).post('/items')
    assert calls == []


def test_struct_default_copy_preserves_user_copy_count() -> None:
    calls = []

    class Record(msgspec.Struct):
        value: int

        def __deepcopy__(self, memo: Any) -> Any:
            calls.append('copy')
            return Record(self.value + len(calls))

    app = register(Record(7))
    calls.clear()
    response = TestClient(app).post('/items')
    assert response.status_code == 200 and response.json() == 8
    assert calls == ['copy']
