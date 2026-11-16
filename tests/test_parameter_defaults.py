"""Parameter defaults and their factories must not trigger model copying."""

from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter


@pytest.mark.parametrize('marker', [Query, Header, Cookie])
@pytest.mark.parametrize('dependency', [False, True])
@pytest.mark.parametrize('late', [False, True])
def test_parameter_defaults_reject_models_before_copy(
    marker: Any, dependency: bool, late: bool
) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model copy')
            return super().__deepcopy__(memo)

    value = External(value=1)
    default: Any = {'items': [] if late else [value]}
    annotation = Annotated[Any, marker()]
    router = MsgspecRouter()

    async def source(value: annotation = default) -> None:
        pass

    if dependency:

        async def endpoint(value: Annotated[Any, Depends(source)]) -> None:
            pass
    else:
        endpoint = source

    if late:
        router.get('/value')(endpoint)
        app = FastAPI()
        app.include_router(router)
        default['items'].append(value)
        with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
            TestClient(app).get('/value')
    else:
        with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
            router.get('/value')(endpoint)
            app = FastAPI()
            app.include_router(router)
    assert calls == []


@pytest.mark.parametrize('marker', [Query, Header, Cookie])
def test_parameter_factory_result_is_checked_before_copy(marker: Any) -> None:
    calls = []
    factories = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model copy')
            return super().__deepcopy__(memo)

    value = External(value=1)

    def factory() -> Any:
        factories.append(True)
        return {'items': [value]}

    annotation = Annotated[Any, marker(default_factory=factory)]
    router = MsgspecRouter()

    @router.get('/value')
    async def endpoint(value: annotation) -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    assert factories == []
    with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
        TestClient(app).get('/value')
    assert factories == [True] and calls == []


@pytest.mark.parametrize('marker', [Query, Header, Cookie])
def test_supported_factory_keeps_native_evaluation_count(marker: Any) -> None:
    def run(router_class: type[APIRouter]) -> tuple[int, int]:
        calls = []

        def factory() -> dict[str, Any]:
            calls.append(True)
            return {'items': [1, 2]}

        annotation = Annotated[Any, marker(default_factory=factory)]
        router = router_class()

        @router.get('/value')
        async def endpoint(value: annotation) -> int:
            return len(value['items'])

        app = FastAPI()
        app.include_router(router)
        assert calls == []
        response = TestClient(app).get('/value')
        assert response.status_code == 200
        return response.json(), len(calls)

    native = run(APIRouter)
    assert native[0] == 2 and native[1] > 0
    assert run(MsgspecRouter) == native


@pytest.mark.parametrize('marker', [Query, Header, Cookie])
def test_parameter_example_models_are_rejected_before_native_setup(marker: Any) -> None:
    calls = []

    class External(BaseModel):
        value: int

        def __deepcopy__(self, memo: Any = None) -> Any:
            calls.append('model copy')
            return super().__deepcopy__(memo)

    annotation = Annotated[int, marker(examples=[External(value=1)])]
    router = MsgspecRouter()

    async def endpoint(value: annotation) -> None:
        pass

    with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
        router.get('/value')(endpoint)
    assert calls == []
