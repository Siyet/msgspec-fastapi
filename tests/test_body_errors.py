"""Structural locations and safe diagnostics without repeating model hooks."""

import sys
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Annotated, Any, Generic, NamedTuple, TypeVar

import msgspec
import pytest
from fastapi import APIRouter, Body, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from typing_extensions import TypedDict

from msgspec_fastapi import MsgspecRouter, install_openapi


class Item(msgspec.Struct):
    number: int
    count: int


class PydItem(BaseModel):
    number: int
    count: int


@pytest.mark.parametrize('method', ['hash', 'equality', 'getattribute'])
def test_diagnostics_do_not_repeat_enum_set_callbacks(method: str) -> None:
    calls = []
    checking = False

    class Hashed(str, Enum):
        first = 'first'

        def __hash__(self) -> int:
            calls.append('hash')
            if len(calls) > 1:
                raise RuntimeError('Hash callback repeated')
            return hash(self.value)

    class Compared(IntEnum):
        first = 0
        second = sys.hash_info.modulus

        def __eq__(self, other: Any) -> bool:
            calls.append('equality')
            if len(calls) > 1:
                raise RuntimeError('Equality callback repeated')
            return int.__eq__(self, other)

        __hash__ = int.__hash__

    class LookedUp(Enum):
        first = 'first'

        def __getattribute__(self, name: str) -> Any:
            if checking and name == '_name_':
                calls.append('getattribute')
                if len(calls) > 1:
                    raise RuntimeError('Getter callback repeated')
            return object.__getattribute__(self, name)

    target = {'hash': Hashed, 'equality': Compared, 'getattribute': LookedUp}[method]

    class Payload(msgspec.Struct):
        choices: set[target]
        count: int

    test_client = client(Payload)
    checking = True
    calls.clear()
    values = [0, sys.hash_info.modulus] if method == 'equality' else ['first']
    response = test_client.post('/value', json={'choices': values, 'count': 'bad'})
    assert response.status_code == 422
    assert calls == [method]
    assert response.json()['detail'][0]['loc'] == ['body']


def client(
    annotation: Any, router_class: type[APIRouter] = MsgspecRouter, *, embed: bool = False
) -> TestClient:
    router = router_class()

    @router.post('/value')
    async def endpoint(value: Annotated[annotation, Body(embed=embed, alias='payload')]) -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    if router_class is MsgspecRouter:
        install_openapi(app)
    return TestClient(app)


@pytest.mark.parametrize('router_class,model', [(APIRouter, PydItem), (MsgspecRouter, Item)])
@pytest.mark.parametrize('embed', [False, True])
def test_multiple_nested_errors_have_structured_locations(
    router_class: type[APIRouter], model: Any, embed: bool
) -> None:
    payload = [{'number': 'bad', 'count': 'bad'}, {'count': 'bad'}]
    result = client(list[model], router_class, embed=embed).post(
        '/value', json={'payload': payload} if embed else payload
    )
    assert result.status_code == 422
    prefix = ['body', 'payload'] if embed else ['body']
    assert [e['loc'] for e in result.json()['detail']] == [
        prefix + [0, 'number'],
        prefix + [0, 'count'],
        prefix + [1, 'number'],
        prefix + [1, 'count'],
    ]


def test_encoded_alias_is_one_location_component() -> None:
    class Aliased(msgspec.Struct):
        value: int = msgspec.field(name='a.b[0]')

    result = client(list[Aliased]).post('/value', json=[{'a.b[0]': 'bad'}])
    assert result.status_code == 422
    assert result.json()['detail'][0]['loc'] == ['body', 0, 'a.b[0]']


def test_raw_field_does_not_produce_a_false_validation_error() -> None:
    class Payload(msgspec.Struct):
        raw: msgspec.Raw
        count: int

    result = client(Payload).post('/value', json={'raw': {'a': 1}, 'count': 'bad'})
    assert result.status_code == 422
    assert [e['loc'] for e in result.json()['detail']] == [['body', 'count']]


def test_model_post_init_is_not_repeated() -> None:
    calls = []

    class Child(msgspec.Struct):
        number: int

        def __post_init__(self) -> None:
            calls.append(self.number)

    class Parent(msgspec.Struct):
        child: Child
        count: int

    result = client(Parent).post('/value', json={'child': {'number': 1}, 'count': 'bad'})
    assert result.status_code == 422 and calls == [1]
    assert result.json()['detail'][0]['loc'] == ['body']


def test_generic_specialization_preserves_the_original_hook_failure() -> None:
    T = TypeVar('T')
    calls = []

    class Box(msgspec.Struct, Generic[T]):
        value: T

    class Checked(msgspec.Struct):
        number: int

        def __post_init__(self) -> None:
            calls.append(self.number)
            if self.number < 0:
                raise ValueError('Semantic invariant failed')

    class Parent(msgspec.Struct):
        first: Box[int]
        second: Box[Checked]
        count: int

    result = client(Parent).post(
        '/value', json={'first': {'value': 1}, 'second': {'value': {'number': -1}}, 'count': 'bad'}
    )
    assert result.status_code == 422 and calls == [-1]
    assert 'Semantic invariant failed' in result.json()['detail'][0]['msg']


@pytest.mark.parametrize('alias', [False, True])
def test_tuple_diagnostics_do_not_repeat_nested_hooks(alias: bool) -> None:
    calls = []

    class Child(msgspec.Struct):
        number: int

        def __post_init__(self) -> None:
            calls.append(self.number)

    class Pair(NamedTuple):
        child: Child

    if alias:
        import typing

        if not hasattr(typing, 'TypeAliasType'):
            pytest.skip('Native type aliases require Python 3.12')
        annotation = typing.TypeAliasType('AliasedTuple', tuple[Child])
    else:
        annotation = Pair

    class Parent(msgspec.Struct):
        first: annotation
        count: int

    result = client(Parent).post('/value', json={'first': [{'number': 1}], 'count': 'bad'})
    assert result.status_code == 422 and calls == [1]


def test_container_deduplication_failure_is_not_lost() -> None:
    class Payload(msgspec.Struct):
        values: Annotated[set[Decimal], msgspec.Meta(min_length=2)]
        count: int

    result = client(Payload).post('/value', json={'values': ['1', '1.0'], 'count': 'bad'})
    assert result.status_code == 422
    assert 'length' in result.json()['detail'][0]['msg']
    assert 'values' in result.json()['detail'][0]['msg']


def test_empty_tuple_length_error_is_not_lost() -> None:
    class Payload(msgspec.Struct):
        values: tuple[()]
        count: int

    result = client(Payload).post('/value', json={'values': [1], 'count': 'bad'})
    assert result.status_code == 422
    assert 'length' in result.json()['detail'][0]['msg']


def test_dataclass_defaults_are_not_constructed_twice() -> None:
    calls = []

    def default() -> int:
        calls.append(True)
        return 1

    from dataclasses import field

    @dataclass
    class Child:
        number: int = field(default_factory=default)

    class Parent(msgspec.Struct):
        child: Child
        count: int

    result = client(Parent).post('/value', json={'child': {}, 'count': 'bad'})
    assert result.status_code == 422 and calls == [True]
    assert [e['loc'] for e in result.json()['detail']] == [['body', 'count']]


def test_typed_dict_and_dataclass_fields_have_locations() -> None:
    class Mapping(TypedDict):
        number: int
        count: int

    @dataclass
    class Record:
        number: int
        count: int

    for annotation in (Mapping, Record):
        result = client(annotation).post('/value', json={'number': 'bad', 'count': 'bad'})
        assert result.status_code == 422
        assert [e['loc'] for e in result.json()['detail']] == [
            ['body', 'number'],
            ['body', 'count'],
        ]


def test_numeric_dictionary_keys_keep_original_locations() -> None:
    result = client(dict[int, int]).post('/value', json={'1': 'bad', 'wrong': 'bad'})
    assert result.status_code == 422
    assert [e['loc'] for e in result.json()['detail']] == [
        ['body', '1'],
        ['body', 'wrong', '[key]'],
        ['body', 'wrong'],
    ]


def test_tagged_union_uses_the_selected_struct() -> None:
    class First(msgspec.Struct, tag='first', tag_field='kind'):
        number: int
        count: int

    class Second(msgspec.Struct, tag='second', tag_field='kind'):
        text: str

    c = client(First | Second)
    result = c.post('/value', json={'kind': 'first', 'number': 'bad', 'count': 'bad'})
    assert result.status_code == 422
    assert [e['loc'] for e in result.json()['detail']] == [['body', 'number'], ['body', 'count']]
    assert c.post('/value', json={'kind': 'second', 'text': 'ok'}).status_code == 200
    wrong = c.post('/value', json={'kind': 'wrong'})
    assert wrong.status_code == 422 and wrong.json()['detail'][0]['loc'] == ['body', 'kind']


@pytest.mark.parametrize('recover', [False, True])
def test_enum_missing_hook_is_not_repeated(recover: bool) -> None:
    from enum import Enum

    calls = []

    class Choice(str, Enum):
        first = 'first'

        @classmethod
        def _missing_(cls, value: object) -> Any:
            calls.append(value)
            return cls.first if recover else None

    class Payload(msgspec.Struct):
        choice: Choice
        count: int

    result = client(Payload).post('/value', json={'choice': 'other', 'count': 'bad'})
    assert result.status_code == 422 and calls == ['other']
    assert result.json()['detail'][0]['loc'] == ['body']
    assert ('count' if recover else 'enum') in result.json()['detail'][0]['msg']


def test_legacy_empty_tuple_length_error_is_not_lost() -> None:
    from typing import Tuple

    class Payload(msgspec.Struct):
        values: Tuple[()]
        count: int

    result = client(Payload).post('/value', json={'values': [1], 'count': 'bad'})
    assert result.status_code == 422
    assert 'length' in result.json()['detail'][0]['msg']
    assert 'values' in result.json()['detail'][0]['msg']


@pytest.mark.parametrize('key_type,values', [(int, {'-0': 1, '0': 2}), (float, {'1': 1, '1.0': 2})])
def test_coerced_dictionary_key_length_error_is_not_lost(key_type: Any, values: Any) -> None:
    class Payload(msgspec.Struct):
        values: Annotated[dict[key_type, int], msgspec.Meta(min_length=2)]
        count: int

    result = client(Payload).post('/value', json={'values': values, 'count': 'bad'})
    assert result.status_code == 422
    assert 'length' in result.json()['detail'][0]['msg']
    assert 'values' in result.json()['detail'][0]['msg']
