"""Copy predicates retain ordinary Python instance-check behavior."""

import copy
import copyreg
import dataclasses
from typing import Annotated, Any

import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient

from msgspec_fastapi import MsgspecRouter, defaults


def operation_with(copier: Any, operation: Any, value: Any) -> Any:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(defaults, 'deepcopy', copier)
        return operation(value)


@pytest.mark.parametrize('raises', [False, True])
def test_copied_state_class_property_matches_stdlib(raises: bool) -> None:
    def run(copier: Any) -> Any:
        events = []
        original_state = {'value': 7}
        old = copy._deepcopy_dispatch[dict]

        class State:
            @property
            def __class__(self) -> Any:
                events.append('class')
                if raises:
                    raise ValueError('state class')
                return tuple

            def __len__(self) -> int:
                events.append('len')
                return 2

            def __iter__(self) -> Any:
                events.append('iter')
                return iter(({'value': 8}, None))

        @dataclasses.dataclass
        class Product:
            value: int = 0

        @dataclasses.dataclass
        class Original:
            value: int

        def copied_state(value: Any, memo: Any) -> Any:
            return State() if value is original_state else old(value, memo)

        def construct() -> Any:
            copyreg.dispatch_table.pop(Original, None)
            copy._deepcopy_dispatch[dict] = copied_state
            return Product()

        def reduced(value: Any) -> Any:
            return construct, (), original_state

        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: Any) -> Any:
                copyreg.dispatch_table[Original] = reduced
                return self

        def body_route(value: Any) -> Any:
            router = MsgspecRouter()

            async def endpoint(items: Annotated[Any, Body()] = value) -> int:
                return items[1].value

            router.post('/copy')(endpoint)
            app = FastAPI()
            app.include_router(router)
            response = TestClient(app).post('/copy')
            assert response.status_code == 200
            return response.json()

        try:
            result = operation_with(copier, body_route, [Setter(0), Original(7)])
            return result, events
        except ValueError as error:
            return str(error), events
        finally:
            copy._deepcopy_dispatch[dict] = old
            copyreg.dispatch_table.pop(Original, None)

    expected = ('state class', ['class']) if raises else (8, ['class', 'len', 'iter'])
    assert run(copy.deepcopy) == expected
    assert run(defaults.deepcopy) == expected


@pytest.mark.parametrize('raises', [False, True])
def test_reduction_result_class_property_matches_stdlib(raises: bool) -> None:
    def run(copier: Any) -> Any:
        events = []

        @dataclasses.dataclass
        class Original:
            value: int

        class Proxy:
            @property
            def __class__(self) -> Any:
                events.append('class')
                if raises:
                    raise ValueError('reduction class')
                return str

        def reduced(value: Any) -> Any:
            copyreg.dispatch_table.pop(Original, None)
            return Proxy()

        @dataclasses.dataclass
        class Setter:
            value: int

            def __deepcopy__(self, memo: Any) -> Any:
                copyreg.dispatch_table[Original] = reduced
                return self

        original = Original(7)
        try:
            result = operation_with(copier, defaults.copy_metadata, [Setter(0), original])
            return result[1] is original, events
        except ValueError as error:
            return str(error), events
        finally:
            copyreg.dispatch_table.pop(Original, None)

    expected = ('reduction class', ['class']) if raises else (True, ['class'])
    assert run(copy.deepcopy) == expected
    assert run(defaults.deepcopy) == expected
