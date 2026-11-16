"""Copied route metadata must not trigger model data processing."""

import json
import sys
from typing import Annotated, Any, List, Optional, Union

import msgspec
import pytest
from fastapi import Body, FastAPI
from fastapi.exceptions import FastAPIDeprecationWarning
from pydantic import BaseModel

from msgspec_fastapi import MsgspecRouter, install_openapi


class Failure(BaseModel):
    detail: str


def model_copies(frame: Any, event: str, arg: Any, calls: list[str]) -> None:
    if (
        event == 'call'
        and frame.f_code.co_filename.endswith('/pydantic/main.py')
        and frame.f_code.co_name == '__deepcopy__'
    ):
        calls.append('model copy')


@pytest.mark.parametrize(
    'where',
    [
        'responses',
        'openapi_extra',
        'body_examples',
        'body_examples_callback',
        'body_example',
        'body_openapi_examples',
        'body_schema_dict',
    ],
)
def test_metadata_model_values_are_rejected_before_copy(where: str) -> None:
    value = Failure(detail='private')
    router = MsgspecRouter()
    calls = []
    previous = sys.getprofile()

    class Payload(msgspec.Struct):
        value: int

    def customize(schema: dict[str, Any]) -> None:
        schema['description'] = 'Example'

    async def ordinary() -> None:
        pass

    body_options = {
        'body_examples': {'examples': [value]},
        'body_examples_callback': {'examples': [value], 'json_schema_extra': customize},
        'body_example': {'example': value},
        'body_openapi_examples': {'openapi_examples': {'sample': {'value': value}}},
        'body_schema_dict': {'json_schema_extra': {'examples': [value]}},
    }.get(where, {})
    if where == 'body_example':
        with pytest.warns(FastAPIDeprecationWarning, match='deprecated'):
            info = Body(**body_options)
    else:
        info = Body(**body_options)
    annotation = Annotated[Payload, info]

    async def body(payload: annotation) -> None:
        pass

    kwargs = {
        'responses': {'responses': {400: {'content': {'application/json': {'example': value}}}}},
        'openapi_extra': {'openapi_extra': {'x-example': {'items': [value]}}},
    }.get(where, {})
    try:

        def observe(frame: Any, event: str, arg: Any) -> None:
            model_copies(frame, event, arg, calls)

        sys.setprofile(observe)
        with pytest.raises(TypeError, match='Pydantic model behavior is not supported'):
            router.add_api_route(
                '/value', body if where.startswith('body_') else ordinary, **kwargs
            )
    finally:
        sys.setprofile(previous)
    assert calls == []


@pytest.mark.parametrize(
    'shape', ['direct', 'list', 'union', 'typing_list', 'typing_union', 'typing_optional']
)
def test_native_schema_type_references_remain_available(shape: str) -> None:
    annotation = {
        'direct': Failure,
        'list': list[Failure],
        'union': Failure | None,
        'typing_list': List[Failure],
        'typing_union': Union[Failure, int],
        'typing_optional': Optional[Failure],
    }[shape]
    router = MsgspecRouter()

    @router.get('/value', responses={400: {'model': annotation}})
    async def endpoint() -> None:
        pass

    app = FastAPI()
    app.include_router(router)
    install_openapi(app)
    document = app.openapi()
    assert document['components']['schemas']['Failure']['properties']['detail']['type'] == 'string'
    schema = document['paths']['/value']['get']['responses']['400']['content']['application/json'][
        'schema'
    ]
    assert '#/components/schemas/Failure' in json.dumps(schema)
