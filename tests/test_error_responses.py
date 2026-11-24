"""Body error responses preserve FastAPI exception and cleanup behavior."""

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

import msgspec
import pytest
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel, Field

from msgspec_fastapi import MsgspecRoute, MsgspecRouter


class Item(msgspec.Struct):
    count: int


def application(router: MsgspecRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.parametrize('asynchronous', [False, True])
def test_custom_body_error_handler_keeps_exact_exception_type(asynchronous: bool) -> None:
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(items: list[Item]) -> None:
        pass

    app = application(router)
    observed = []

    def handle(request: Request, exc: RequestValidationError) -> Response:
        observed.append((type(exc), len(exc.errors())))
        return JSONResponse({'custom': True}, status_code=418)

    async def async_handle(request: Request, exc: RequestValidationError) -> Response:
        return handle(request, exc)

    app.add_exception_handler(RequestValidationError, async_handle if asynchronous else handle)
    response = TestClient(app).post('/items', json=[{'count': 'bad'}, {'count': 'bad'}])
    assert response.status_code == 418 and response.json() == {'custom': True}
    assert observed == [(RequestValidationError, 2)]


@pytest.mark.parametrize('scope', ['request', 'function'])
def test_yield_dependency_can_translate_body_errors(scope: str) -> None:
    calls = []

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError:
            calls.append('validation')
            raise HTTPException(409, 'Translated validation error')
        finally:
            calls.append('cleanup')

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency, scope=scope)])
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 409
    assert response.json() == {'detail': 'Translated validation error'}
    assert calls == ['validation', 'cleanup']


@pytest.mark.parametrize('scope', ['request', 'function'])
def test_yield_cleanup_precedes_body_error_response(scope: str) -> None:
    calls = []

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        finally:
            calls.append('cleanup')

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency, scope=scope)])
    async def endpoint(items: list[Item]) -> None:
        pass

    app = application(router)

    @app.middleware('http')
    async def observe_response(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        calls.append('response')
        return response

    assert TestClient(app).post('/items', json=[{'count': 'bad'}]).status_code == 422
    assert calls == ['cleanup', 'response']


def test_custom_route_can_intercept_body_error() -> None:
    class CustomRoute(MsgspecRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            handler = super().get_route_handler()

            async def handle(request: Request) -> Response:
                try:
                    return await handler(request)
                except RequestValidationError:
                    return JSONResponse({'route': True}, status_code=409)

            return handle

    router = MsgspecRouter(route_class=CustomRoute)

    @router.post('/items')
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 409 and response.json() == {'route': True}


@pytest.mark.parametrize('patterns', [False, True])
def test_default_body_error_wire_matches_native_handler(patterns: bool) -> None:
    annotation = list[Annotated[str, Field(pattern='^a+$')]] if patterns else list[Item]
    payload = ['a\n', 'кот'] if patterns else [{'count': 'ошибка'}, {'count': []}]

    def build(wrapped: bool) -> FastAPI:
        router = MsgspecRouter()

        @router.post('/items')
        async def endpoint(items: annotation) -> None:
            pass

        app = application(router)
        if wrapped:

            @app.exception_handler(RequestValidationError)
            async def handler(request: Request, exc: RequestValidationError) -> Response:
                return await request_validation_exception_handler(request, exc)

        return app

    native = TestClient(build(True)).post('/items', json=payload)
    actual = TestClient(build(False)).post('/items', json=payload)
    assert actual.status_code == native.status_code == 422
    assert actual.content == native.content
    assert actual.headers == native.headers


def test_handler_registration_changes_remain_visible_to_later_routes() -> None:
    async def custom(request: Request, exc: RequestValidationError) -> Response:
        return JSONResponse({'registered': True}, status_code=418)

    async def dependency(request: Request) -> None:
        handlers, _ = request.scope['starlette.exception_handlers']
        handlers[RequestValidationError] = custom

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    app = application(router)

    @app.get('/native')
    async def native(count: int) -> None:
        pass

    with TestClient(app) as client:
        assert client.post('/items', json=[{'count': 'bad'}]).status_code == 418
        assert client.get('/native', params={'count': 'bad'}).status_code == 418


@pytest.mark.parametrize(
    'details',
    ['custom', 'decimal', 'bytes', 'datetime', 'nan', 'infinity', 'float', 'int_subclass'],
)
def test_dependency_enriched_body_errors_use_msgspec_encoding(details: str) -> None:
    import datetime
    from decimal import Decimal

    class Details:
        def __init__(self) -> None:
            self.reason = 'extra'

    class Number(int):
        pass

    calls = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append('serialized')
        return {'private': 'must-not-leak'}

    Details.model_dump = forbidden
    Details.model_dump_json = forbidden
    value = {
        'custom': Details(),
        'decimal': Decimal('1.2500'),
        'bytes': b'hello',
        'datetime': datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        'nan': float('nan'),
        'infinity': float('inf'),
        'float': 1e-7,
        'int_subclass': Number(1),
    }[details]

    def build(wrapped: bool) -> FastAPI:
        async def dependency() -> AsyncIterator[None]:
            try:
                yield
            except RequestValidationError as exc:
                exc.errors()[0]['ctx'] = {'details': value}
                raise

        router = MsgspecRouter()

        @router.post('/items', dependencies=[Depends(dependency)])
        async def endpoint(items: list[Item]) -> None:
            pass

        app = application(router)
        if wrapped:

            @app.exception_handler(RequestValidationError)
            async def handler(request: Request, exc: RequestValidationError) -> Response:
                return await request_validation_exception_handler(request, exc)

        return app

    native = TestClient(build(True), raise_server_exceptions=False).post(
        '/items', json=[{'count': 'bad'}]
    )
    actual = TestClient(build(False), raise_server_exceptions=False).post(
        '/items', json=[{'count': 'bad'}]
    )
    assert native.status_code == (500 if details in ('nan', 'infinity') else 422)
    if details in ('custom', 'int_subclass'):
        assert actual.status_code == 500
        assert 'must-not-leak' not in actual.text
    else:
        expected = {
            'decimal': '1.2500',
            'bytes': 'aGVsbG8=',
            'datetime': '2026-01-01T00:00:00Z',
            'nan': None,
            'infinity': None,
            'float': 1e-7,
        }[details]
        assert actual.status_code == 422
        assert actual.json() == {
            'detail': [
                {
                    'loc': ['body', 0, 'count'],
                    'msg': 'Expected `int`, got `str`',
                    'type': 'value_error.msgspec',
                    'ctx': {'details': expected},
                }
            ]
        }
    assert calls == []


@pytest.mark.parametrize('canonical', [False, True])
def test_dependency_error_method_is_not_repeated(canonical: bool) -> None:
    calls = []

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError as exc:
            errors = exc.errors()
            if not canonical:
                errors[0]['ctx'] = {'details': {'reason': 'extra'}}

            def changed_errors() -> list[Any]:
                calls.append('errors')
                return errors

            exc.errors = changed_errors
            raise

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 422
    assert calls == ['errors']


def test_rebound_native_error_method_keeps_original_receiver() -> None:
    from types import MethodType

    calls = []

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError as exc:
            errors = exc.errors()
            errors[0]['ctx'] = {'details': {'reason': 'extra'}}

            class Provider:
                @property
                def _errors(self) -> list[Any]:
                    calls.append('errors')
                    if len(calls) > 1:
                        raise RuntimeError('Provider repeated')
                    return errors

            exc.errors = MethodType(RequestValidationError.errors, Provider())
            raise

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 422
    assert calls == ['errors']


def test_custom_error_descriptor_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    native_errors = RequestValidationError.errors

    def changed_errors(exc: RequestValidationError) -> Callable[..., Any]:
        calls.append('get')
        if len(calls) > 1:
            raise RuntimeError('Descriptor repeated')
        return lambda: native_errors(exc)

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError:
            monkeypatch.setattr(RequestValidationError, 'errors', property(changed_errors))
            raise

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 422
    assert calls == ['get']


def test_custom_error_location_does_not_repeat_metaclass_comparisons() -> None:
    calls = []

    class Meta(type):
        __hash__ = type.__hash__

        def __eq__(cls, other: Any) -> bool:
            calls.append('eq')
            return False

    class Location(metaclass=Meta):
        def __init__(self) -> None:
            self.reason = 'extra'

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError as exc:
            exc.errors()[0]['loc'] = Location()
            raise

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router), raise_server_exceptions=False).post(
        '/items', json=[{'count': 'bad'}]
    )
    assert response.status_code == 500
    assert 'extra' not in response.text
    assert calls == []


def test_custom_exception_namespace_is_not_iterated() -> None:
    calls = []

    class Namespace(dict):
        def __iter__(self) -> Any:
            calls.append('iter')
            raise RuntimeError('Namespace iteration')

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError as exc:
            exc.__dict__ = Namespace(exc.__dict__)
            raise

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 422
    assert calls == []


def test_body_error_serialization_does_not_repeat_body_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original = Request.body

    async def read(request: Request) -> bytes:
        calls.append('read')
        if len(calls) > 1:
            raise RuntimeError('Body reader repeated')
        return await original(request)

    monkeypatch.setattr(Request, 'body', read)
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 422
    assert calls == ['read']


def test_custom_body_bytes_do_not_receive_an_added_truth_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    original = Request.body

    class CustomBytes(bytes):
        def __bool__(self) -> bool:
            calls.append('truth')
            if len(calls) > 1:
                raise RuntimeError('Body truth check repeated')
            return True

    async def read(request: Request) -> bytes:
        return CustomBytes(await original(request))

    monkeypatch.setattr(Request, 'body', read)
    router = MsgspecRouter()

    @router.post('/items')
    async def endpoint(items: list[Item]) -> None:
        pass

    response = TestClient(application(router)).post('/items', json=[{'count': 'bad'}])
    assert response.status_code == 422
    assert calls == ['truth']


@pytest.mark.parametrize('kind', ['model', 'secret'])
def test_default_error_encoding_never_extracts_pydantic_secrets(kind: str) -> None:
    from pydantic import SecretStr

    calls = []

    class PrivateModel(BaseModel):
        secret: str

        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append('model_dump')
            return {'secret': self.secret}

        def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
            calls.append('model_dump_json')
            return self.secret

    class PrivateSecret(SecretStr):
        def get_secret_value(self) -> str:
            calls.append('secret')
            return super().get_secret_value()

        def __str__(self) -> str:
            calls.append('str')
            return super().__str__()

    value = (
        PrivateModel(secret='must-not-leak') if kind == 'model' else PrivateSecret('must-not-leak')
    )

    async def dependency() -> AsyncIterator[None]:
        try:
            yield
        except RequestValidationError as exc:
            exc.errors()[0]['ctx'] = {'details': value}
            raise

    router = MsgspecRouter()

    @router.post('/items', dependencies=[Depends(dependency)])
    async def endpoint(items: list[Item]) -> None:
        pass

    calls.clear()
    response = TestClient(application(router), raise_server_exceptions=False).post(
        '/items', json=[{'count': 'bad'}]
    )
    assert response.status_code == 500
    assert calls == []
    assert 'must-not-leak' not in response.text


class NegativeError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.token = 'attribute'


@pytest.mark.parametrize('error', [ValueError, NegativeError])
def test_validator_exception_in_error_context_is_rendered_as_empty_object(error: type) -> None:
    def reject_negative(value: int) -> int:
        if value < 0:
            raise error('negative')
        return value

    app = FastAPI()
    for router, path in [(MsgspecRouter(), '/msgspec'), (APIRouter(), '/fastapi')]:

        @router.post(path)
        async def endpoint(x: Annotated[int, Form(), AfterValidator(reject_negative)]) -> None:
            pass

        app.include_router(router)
    client = TestClient(app)
    expected = client.post('/fastapi', data={'x': '-1'}).json()['detail']
    response = client.post('/msgspec', data={'x': '-1'})
    assert response.status_code == 422
    assert response.json()['detail'] == [{**expected[0], 'ctx': {'error': {}}}]


class Upload(BaseModel):
    a: int
    docs: list[UploadFile]


@pytest.mark.parametrize(
    ('annotation', 'names', 'rendered'),
    [
        (Annotated[int, Form()], ['x'], {}),
        (Annotated[list[UploadFile], File(max_length=1)], ['x', 'x'], [{}, {}]),
        (Annotated[Upload, Form()], ['docs', 'docs'], {'docs': [{}, {}]}),
    ],
)
def test_files_sent_in_form_are_rendered_as_empty_objects(
    annotation: Any, names: list[str], rendered: Any
) -> None:
    app = FastAPI()
    for router, path in [(MsgspecRouter(), '/msgspec'), (APIRouter(), '/fastapi')]:

        @router.post(path)
        async def endpoint(x: annotation) -> None:
            pass

        app.include_router(router)
    client = TestClient(app)
    files = [(name, ('a.txt', b'secret', 'text/plain')) for name in names]
    expected = client.post('/fastapi', files=files).json()['detail']
    response = client.post('/msgspec', files=files)
    assert response.status_code == 422
    assert response.json()['detail'] == [{**expected[0], 'input': rendered}]
