"""Copied parameter default getters retain the existing live metadata capture."""

from copy import copy, deepcopy
from typing import Annotated, Any

import pytest
from fastapi import Query
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter


@pytest.mark.parametrize('copier', [copy, deepcopy])
def test_copied_default_getter_retains_late_default_and_factory(copier: Any) -> None:
    router = MsgspecRouter()

    @router.get('/value')
    async def endpoint(value: Annotated[Any, Query()] = 7) -> None:
        pass

    field = router.routes[0].dependant.query_params[0]
    copied = copier(field.get_default)
    field.field_info.default = [9]
    value = copied()
    assert value == [9] and value is not field.field_info.default
    calls = []

    def factory() -> list[int]:
        calls.append('factory')
        return [11]

    field.field_info.default_factory = factory
    assert copied() == [11]
    assert calls == ['factory']


@pytest.mark.parametrize('copier', [copy, deepcopy])
def test_copying_default_getter_does_not_copy_pydantic_default(copier: Any) -> None:
    calls = []

    class Model(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model-copy')
            return super().__deepcopy__(memo)

    router = MsgspecRouter()

    @router.get('/value')
    async def endpoint(value: Annotated[Any, Query()] = 7) -> None:
        pass

    field = router.routes[0].dependant.query_params[0]
    field.field_info.default = Model(value=1)
    copied = copier(field.get_default)
    with pytest.raises(TypeError, match='Pydantic model behavior'):
        copied()
    assert calls == []
