# msgspec-fastapi

An experimental FastAPI router that uses msgspec for JSON request bodies and responses.

This is a prototype. It is not published on PyPI, its API may change, and it relies on
private FastAPI internals, so it is pinned to FastAPI 0.142.2 to 0.143.x. It is not an
official msgspec release; feedback is welcome in the issues.

## Usage

```python
import msgspec
from fastapi import FastAPI
from msgspec_fastapi import MsgspecRouter, install_openapi


class City(msgspec.Struct):
    name: str
    population: int


router = MsgspecRouter()


@router.post('/cities')
async def create_city(city: City) -> City:
    return city


app = FastAPI()
app.include_router(router)
install_openapi(app, router)
```

## What it does

- A msgspec body parameter (a Struct or another type msgspec can decode) is decoded from
  the raw request bytes by a `msgspec.json.Decoder` built once at route registration.
  A non-JSON content type returns 415, malformed JSON returns 400, and a missing or
  invalid body returns 422. Bodies are decoded in msgspec strict mode: for example, the
  JSON string `"1"` is not accepted for an `int` field, unlike FastAPI with Pydantic.
- A msgspec response type, from the return annotation or `response_model`, is encoded
  with msgspec. An instance of exactly the declared Struct class is encoded without
  revalidation, as FastAPI does for a Pydantic `response_model`; other values are
  validated against the declared type. Nested values and type parameters of a trusted
  instance are not checked.
- Responses of routes without a return annotation or `response_model` are also encoded
  with msgspec, so the output can differ from FastAPI's `jsonable_encoder` (for example,
  `bytes` are base64-encoded and `Decimal` is encoded as a string).
- Path, query, header and cookie parameters keep FastAPI extraction, aliases and
  defaults, but are converted with `msgspec.convert(strict=False)` instead of Pydantic,
  so coercion differs for some inputs (for example, `" 1.5"` is rejected for `float`
  and `"nan"` is accepted for `Decimal`). Parameter types must be supported by msgspec;
  Pydantic-specific types such as `AnyUrl` raise `TypeError` at route registration.
- `install_openapi` adds the msgspec JSON schemas to the OpenAPI document. It is required
  for a valid document: call it once after including the routers, passing every
  `MsgspecRouter` that is not included directly in another `MsgspecRouter`.
- A Pydantic body or response model is left to FastAPI; the route parameters are still
  validated with msgspec.

## Limitations

- Only one msgspec body parameter per route, and it cannot be combined with other
  FastAPI body fields. Only JSON bodies are supported: `application/json`,
  `application/*+json`, or a request without a content type.
- Structs, alone or inside containers, are not supported as path, query, header or
  cookie parameters.
- `response_model_include`, `response_model_exclude`, `response_model_exclude_*` and
  `response_model_by_alias=False` are rejected for typed msgspec responses.

## Development

```sh
uv run --group test pytest
```
