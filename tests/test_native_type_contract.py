"""Declared data must remain decodable without external model behavior."""

import dataclasses
from typing import Annotated, Any, Generic, NamedTuple, TypeVar

import msgspec
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, model_validator
from pydantic.dataclasses import dataclass as validated_dataclass
from typing_extensions import TypedDict

from msgspec_fastapi import MsgspecRouter

T = TypeVar('T')


class Box(msgspec.Struct, Generic[T]):
    value: T


class Tree(msgspec.Struct):
    value: int
    children: list['Tree']


@dataclasses.dataclass
class DataclassTree:
    value: int
    children: list['DataclassTree']


class DictionaryTree(TypedDict):
    value: int
    children: list['DictionaryTree']


def register_data(annotation: Any, position: str) -> None:
    router = MsgspecRouter()
    if position == 'body':

        async def endpoint(value: annotation) -> None:
            raise AssertionError('Registration must not execute an endpoint')

        router.post('/value')(endpoint)
    else:

        async def endpoint() -> annotation:
            raise AssertionError('Registration must not execute an endpoint')

        router.get('/value')(endpoint)


@pytest.mark.parametrize('position', ['body', 'response'])
@pytest.mark.parametrize('shape', ['struct', 'dataclass', 'dictionary', 'tuple', 'generic'])
def test_schema_override_cannot_enable_custom_decoding(shape: str, position: str) -> None:
    constructed = []

    class External:
        def __new__(cls) -> 'External':
            constructed.append('instance')
            return super().__new__(cls)

    opaque = Annotated[External, msgspec.Meta(extra_json_schema={'type': 'object'})]
    if shape == 'struct':
        annotation = msgspec.defstruct('Payload', [('value', opaque)])
    elif shape == 'dataclass':
        annotation = dataclasses.make_dataclass('Payload', [('value', opaque)])
    elif shape == 'dictionary':
        annotation = TypedDict('Payload', {'value': opaque})
    elif shape == 'tuple':
        annotation = NamedTuple('Payload', [('value', opaque)])
    else:
        annotation = Box[list[opaque]]
    with pytest.raises(TypeError):
        register_data(annotation, position)
    assert constructed == []


@pytest.mark.parametrize('position', ['body', 'response'])
@pytest.mark.parametrize('shape', ['direct', 'list', 'generic'])
def test_validated_dataclass_is_rejected_without_bypassing_its_validator(
    shape: str, position: str
) -> None:
    calls = []

    @validated_dataclass
    class Validated:
        value: int

        @model_validator(mode='after')
        def positive(self) -> Any:
            calls.append('validate')
            if self.value < 0:
                raise ValueError('negative value')
            return self

    with pytest.raises(ValueError, match='negative value'):
        Validated(-1)
    assert calls == ['validate']
    calls.clear()
    annotation = {'direct': Validated, 'list': list[Validated], 'generic': Box[Validated]}[shape]
    with pytest.raises(TypeError):
        register_data(annotation, position)
    assert calls == []


@pytest.mark.parametrize('shape', ['direct', 'list', 'struct'])
def test_model_rejection_precedes_dynamic_model_attribute_lookup(shape: str) -> None:
    calls = []

    class RecordingMeta(type(BaseModel)):
        def __getattr__(cls, name: str) -> Any:
            calls.append(name)
            return super().__getattr__(name)

    class ExternalModel(BaseModel, metaclass=RecordingMeta):
        value: int

    annotation = {
        'direct': ExternalModel,
        'list': list[ExternalModel],
        'struct': msgspec.defstruct('Payload', [('value', ExternalModel)]),
    }[shape]
    calls.clear()
    with pytest.raises(TypeError):
        register_data(annotation, 'response')
    assert calls == []


@pytest.mark.parametrize(
    'shape',
    [
        'dataclass',
        'dictionary',
        'tuple',
        'generic',
        'struct_tree',
        'dataclass_tree',
        'dictionary_tree',
    ],
)
def test_native_nested_and_recursive_data_remains_supported(shape: str) -> None:
    @dataclasses.dataclass
    class Data:
        value: int

    class Dictionary(TypedDict):
        value: int

    class TupleData(NamedTuple):
        value: int

    annotation = {
        'dataclass': Data,
        'dictionary': Dictionary,
        'tuple': TupleData,
        'generic': Box[int],
        'struct_tree': Tree,
        'dataclass_tree': DataclassTree,
        'dictionary_tree': DictionaryTree,
    }[shape]
    payload: Any = [7] if shape == 'tuple' else {'value': 7}
    if shape.endswith('_tree'):
        payload = {'value': 7, 'children': [{'value': 8, 'children': []}]}
    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(value: annotation) -> annotation:
        return value

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post('/value', json=payload)
    assert response.status_code == 200
    assert response.json() == payload


def test_registration_does_not_construct_dataclass_defaults() -> None:
    calls = []

    def default() -> int:
        calls.append('default')
        return 7

    @dataclasses.dataclass
    class Data:
        value: int = dataclasses.field(default_factory=default)

        def __post_init__(self) -> None:
            calls.append('post-init')

    router = MsgspecRouter()

    @router.post('/value')
    async def endpoint(value: Data) -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    assert calls == []
    response = TestClient(app).post('/value', json={})
    assert response.status_code == 200
    assert calls == ['default', 'post-init']


@pytest.mark.parametrize('position', ['body', 'response'])
def test_legacy_validated_dataclass_does_not_bypass_its_validator(position: str) -> None:
    from pydantic.v1 import validator
    from pydantic.v1.dataclasses import dataclass as legacy_dataclass

    calls = []

    @legacy_dataclass
    class Legacy:
        value: int

        @validator('value', allow_reuse=True)
        def positive(cls, value: int) -> int:
            calls.append('validate')
            if value < 0:
                raise ValueError('negative value')
            return value

    with pytest.raises(ValueError, match='negative value'):
        Legacy(-1)
    assert calls == ['validate']
    calls.clear()
    with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
        register_data(Legacy, position)
    assert calls == []
