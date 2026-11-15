"""Constraint carriers must not silently supply unsupported field behavior."""

from typing import Annotated, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import Field

from msgspec_fastapi import MsgspecRouter


@pytest.mark.parametrize('position', ['body', 'response'])
@pytest.mark.parametrize(
    'option', ['alias', 'discriminator', 'exclude', 'default', 'default_factory', 'description']
)
def test_mutated_nested_field_options_are_rejected(position: str, option: str) -> None:
    calls = []

    def default() -> str:
        calls.append('factory')
        return 'a'

    field = Field(pattern='^a+$')
    value = {
        'alias': 'external',
        'discriminator': 'kind',
        'exclude': True,
        'default': 'a',
        'default_factory': default,
        'description': 'Additional field semantics',
    }[option]
    setattr(field, option, value)
    annotation = list[Annotated[str, field]]
    router = MsgspecRouter()
    if position == 'body':

        async def endpoint(items: annotation) -> None:
            pass

        with pytest.raises(TypeError):
            router.post('/items')(endpoint)
    else:

        async def endpoint() -> annotation:
            return ['a']

        with pytest.raises(TypeError):
            router.get('/items')(endpoint)
    assert calls == []


def test_nested_field_numeric_constraints_remain_enforced_by_body_decoding() -> None:
    annotation = list[Annotated[int, Field(ge=2, le=6, multiple_of=2)]]
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(items: annotation) -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post('/items', json=[2, 4, 6]).status_code == 200
    result = client.post('/items', json=[2, 3, 8])
    assert result.status_code == 422
    assert [error['loc'] for error in result.json()['detail']] == [['body', 1], ['body', 2]]


def test_unknown_constraint_carrier_is_rejected_without_reading_its_properties() -> None:
    calls = []

    class Carrier:
        @property
        def metadata(self) -> list[Any]:
            calls.append('metadata')
            raise RuntimeError('Unsupported carrier evaluated')

    annotation = list[Annotated[str, Carrier()]]
    router = MsgspecRouter()

    async def endpoint(items: annotation) -> None:
        pass

    with pytest.raises(TypeError):
        router.post('/items')(endpoint)
    assert calls == []
