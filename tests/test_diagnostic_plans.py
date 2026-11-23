"""Wire-level diagnostics preserve validation and user callback boundaries."""

import sys
from enum import Enum
from typing import Annotated, Any

import msgspec
import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient

from msgspec_fastapi import MsgspecRouter


def client(annotation: Any) -> TestClient:
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(items: annotation) -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize('array', [False, True])
def test_list_diagnostics_do_not_construct_original_models(array: bool) -> None:
    calls = []

    def default() -> str:
        calls.append('factory')
        return 'default'

    class Item(msgspec.Struct, array_like=array, tag='item'):
        count: int = msgspec.field(name='n')
        note: str = msgspec.field(default_factory=default)

        def __del__(self) -> None:
            calls.append('destructor')

    payload = (
        [['item', 1], ['item', 'bad']]
        if array
        else [
            {'type': 'item', 'n': 1},
            {'type': 'item', 'n': 'bad'},
        ]
    )
    raw = msgspec.json.encode(payload)
    test_client = client(list[Item])
    calls.clear()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(raw, type=list[Item])
    expected = calls.copy()
    calls.clear()
    response = test_client.post('/items', content=raw, headers={'content-type': 'application/json'})
    assert response.status_code == 422
    assert calls == expected
    assert response.json()['detail'][0]['loc'] == ['body', 1, 1 if array else 'n']


@pytest.mark.parametrize('bad', [b'"\xff"', b'"\\ud800"', b'1e999', b'9' * 5000])
@pytest.mark.parametrize('first', [False, True])
def test_unknown_fields_cannot_hide_json_errors(bad: bytes, first: bool) -> None:
    class Item(msgspec.Struct):
        count: int

    unknown = b'{"count":1,"ignored":' + bad + b'}'
    invalid = b'{"count":"bad"}'
    raw = b'[' + (unknown + b',' + invalid if first else invalid + b',' + unknown) + b']'
    with pytest.raises((msgspec.DecodeError, UnicodeError)) as error:
        msgspec.json.decode(raw)
    response = client(list[Item]).post(
        '/items', content=raw, headers={'content-type': 'application/json'}
    )
    assert response.status_code == 400
    assert response.json()['detail'].startswith('Malformed JSON body: ')
    if not first:
        assert response.json()['detail'] == f'Malformed JSON body: {error.value}'


@pytest.mark.parametrize('value', [b'"\xff"', b'"\xed\xa0\x80"'])
def test_raw_field_must_be_valid_utf8(value: bytes) -> None:
    class Item(msgspec.Struct):
        raw: msgspec.Raw

    response = client(Item).post(
        '/items', content=b'{"raw": ' + value + b'}', headers={'content-type': 'application/json'}
    )
    assert response.status_code == 400
    assert response.json()['detail'].startswith('Malformed JSON body: ')


def test_invalid_earlier_duplicate_field_is_not_accepted() -> None:
    class Item(msgspec.Struct):
        count: int

    response = client(list[Item]).post(
        '/items',
        content=b'[{"count":"bad","count":1}]',
        headers={'content-type': 'application/json'},
    )
    assert response.status_code == 422
    assert 'Expected' in response.json()['detail'][0]['msg']


def test_enum_hash_mutation_after_registration_cannot_repeat_callbacks() -> None:
    calls = []

    class Value(str, Enum):
        first = 'first'

    class Payload(msgspec.Struct):
        choices: set[Value]
        count: int

    test_client = client(Payload)

    def hashing(self: Value) -> int:
        calls.append('hash')
        if len(calls) > 1:
            raise RuntimeError('Hash callback repeated')
        return hash(self.value)

    Value.__hash__ = hashing
    response = test_client.post('/items', json={'choices': ['first'], 'count': 'bad'})
    assert response.status_code == 422 and calls == ['hash']


def test_union_of_model_and_model_container_does_not_repeat_factory() -> None:
    calls = []

    def default() -> int:
        calls.append('factory')
        if len(calls) > 1:
            raise RuntimeError('Factory callback repeated')
        return 1

    class Item(msgspec.Struct):
        value: int = msgspec.field(default_factory=default)

    class Payload(msgspec.Struct):
        items: Item | list[Item]
        count: int

    response = client(Payload).post('/items', json={'items': [{}], 'count': 'bad'})
    assert response.status_code == 422
    assert calls == ['factory']
    assert response.json()['detail'][0]['loc'] == ['body']


def test_field_names_cannot_add_wire_model_destructor_calls() -> None:
    class Item(msgspec.Struct):
        __del__: int
        count: int

    raw = b'[{"__del__":1,"count":1},{"__del__":2,"count":"bad"}]'
    test_client = client(list[Item])
    observed = []
    original = sys.unraisablehook

    def record(error: Any) -> None:
        observed.append(error.exc_type)

    try:
        sys.unraisablehook = record
        with pytest.raises(msgspec.ValidationError):
            msgspec.json.decode(raw, type=list[Item])
        expected = observed.copy()
        observed.clear()
        response = test_client.post(
            '/items', content=raw, headers={'content-type': 'application/json'}
        )
        assert response.status_code == 422
        assert observed == expected
    finally:
        sys.unraisablehook = original


@pytest.mark.parametrize('supplied', [False, True])
def test_embedded_missing_default_keeps_pattern_behavior(supplied: bool) -> None:
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(value: Annotated[str, Body(embed=True, pattern='^a$')] = 'bad') -> str:
        return value

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post('/items', json={'value': 'bad'} if supplied else {})
    assert response.status_code == (422 if supplied else 200)
    if not supplied:
        assert response.json() == 'bad'


@pytest.mark.parametrize('value', [1 << 100, -(1 << 100)])
def test_wrong_shape_errors_preserve_json_number_classification(value: int) -> None:
    class Item(msgspec.Struct):
        count: int

    encoded = msgspec.json.encode(value)
    with pytest.raises(msgspec.ValidationError) as error:
        msgspec.json.decode(encoded, type=Item)
    response = client(list[Item]).post(
        '/items', content=b'[' + encoded + b']', headers={'content-type': 'application/json'}
    )
    assert response.status_code == 422
    assert response.json()['detail'] == [
        {'loc': ['body', 0], 'msg': str(error.value), 'type': 'value_error.msgspec'}
    ]
