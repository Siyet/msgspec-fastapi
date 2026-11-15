import dataclasses
from typing import Annotated, Any

import msgspec
import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter


def application(default: Any) -> FastAPI:
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(value: Annotated[Any, Body()] = default) -> int:
        return value.value

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.parametrize('kind', ['attribute', 'shadowed_slot', 'copy_descriptor'])
def test_dynamic_copy_protocol_rejected_before_hidden_model_copy(kind: str) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    hidden = External(value=1)

    @dataclasses.dataclass
    class Data:
        value: int

    if kind == 'attribute':

        class Default(Data):
            __slots__ = ('extra',)

            def __getattribute__(self, name: str) -> Any:
                if name == 'extra':
                    return hidden
                return super().__getattribute__(name)

        value = Default(7)
        object.__setattr__(value, 'extra', None)
    elif kind == 'shadowed_slot':

        class Slotted(Data):
            __slots__ = ('extra',)

        class Default(Slotted):
            @property
            def extra(self) -> Any:
                return hidden

        value = Default(7)
        Slotted.__dict__['extra'].__set__(value, None)
    else:

        class Default(Data):
            @property
            def __deepcopy__(self) -> Any:
                return hidden.__deepcopy__

        value = Default(7)

    with pytest.raises(TypeError):
        application(value)
    assert calls == []


@pytest.mark.parametrize('kind', ['dataclass', 'frozen_slots', 'struct'])
def test_ordinary_native_default_copy_remains_supported(kind: str) -> None:
    if kind == 'struct':

        class Record(msgspec.Struct):
            value: int
    else:

        @dataclasses.dataclass(frozen=kind == 'frozen_slots', slots=kind == 'frozen_slots')
        class Record:
            value: int

    app = application(Record(7))
    response = TestClient(app).post('/value')
    assert response.status_code == 200 and response.json() == 7


@pytest.mark.parametrize('kind', ['dataclass', 'struct'])
def test_ordinary_python_copy_function_count_is_preserved(kind: str) -> None:
    calls = []

    if kind == 'struct':

        class Record(msgspec.Struct):
            value: int

            def __deepcopy__(self, memo: Any) -> Any:
                calls.append('copy')
                return Record(self.value + 1)
    else:

        @dataclasses.dataclass
        class Record:
            value: int

            def __deepcopy__(self, memo: Any) -> Any:
                calls.append('copy')
                return Record(self.value + 1)

    app = application(Record(7))
    calls.clear()
    response = TestClient(app).post('/value')
    assert response.status_code == 200 and response.json() == 8
    assert calls == ['copy']


@pytest.mark.parametrize('kind', ['enum', 'int_enum', 'str_enum'])
def test_native_enum_default_remains_supported(kind: str) -> None:
    from enum import Enum, IntEnum, StrEnum

    enum_class = {'enum': Enum, 'int_enum': IntEnum, 'str_enum': StrEnum}[kind]
    value = enum_class('Choice', {'value': 7 if kind != 'str_enum' else 'seven'}).value
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(payload: Annotated[Any, Body()] = value) -> Any:
        return payload

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post('/value')
    assert response.status_code == 200 and response.json() == value.value


@pytest.mark.parametrize('kind', ['datetime', 'time', 'zoneinfo'])
def test_temporal_default_native_state_remains_supported(kind: str) -> None:
    import datetime
    from zoneinfo import ZoneInfo

    timezone = datetime.timezone(datetime.timedelta(hours=2))
    value = {
        'datetime': datetime.datetime(2026, 1, 2, tzinfo=timezone),
        'time': datetime.time(12, 30, tzinfo=timezone),
        'zoneinfo': datetime.datetime(2026, 1, 2, tzinfo=ZoneInfo('UTC')),
    }[kind]
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(payload: Annotated[Any, Body()] = value) -> Any:
        return payload

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post('/value')
    assert response.status_code == 200
    assert response.content == msgspec.json.encode(value)
