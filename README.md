# msgspec-fastapi

An experimental FastAPI router that uses msgspec for JSON request bodies and responses.
In one benchmark service, moving from Pydantic models to msgspec Structs with this router
increased throughput by 68% on a route with a large JSON body and by 11% to 13% on
database-backed routes; see the Benchmarks section below.

This is a prototype. Its API may change, and it relies on private FastAPI internals, so
it is pinned to FastAPI 0.142.2 to 0.143.x. It is a personal project, not part of
msgspec; feedback is welcome in the issues.

## Installation

```sh
pip install msgspec-fastapi
```

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

## Benchmarks

The router was measured in a small reference-data service on a cloud VM with dedicated
vCPUs (October 2026). Each row compares the same service on FastAPI with Pydantic models
against the same service with msgspec Structs and `MsgspecRouter`. The database queries
(SQLAlchemy ORM), event loop and server settings are the same on both sides, and there
is no cache.

| Request | Requests per second | RPS change | CPU time per response | CPU change |
|---|---|---|---|---|
| `POST /cities/validate`, a JSON body of 100 cities (23 KB), no database | 1,610 → 2,703 | **+67.9%** [+67.0%, +68.7%] | 621 → 370 µs | **-40.4%** [-40.7%, -40.1%] |
| `GET /cities?limit=100`, a page of 100 cities read from PostgreSQL | 514 → 569 | **+10.7%** [+9.9%, +11.5%] | 1,944 → 1,757 µs | **-9.7%** [-10.3%, -9.0%] |
| `POST /cities/search`, small JSON filter bodies, a page of 100 cities | 447 → 505 | **+12.9%** [+12.1%, +13.7%] | 2,237 → 1,981 µs | **-11.4%** [-12.1%, -10.8%] |

The first route returns a constant 16-byte response (`{"accepted":100}`), so the
difference on this route comes mostly from request decoding and validation, and the gain
is largest there. On routes that read from a database, API CPU time per response falls by
about 190 to 260 µs out of roughly 2 ms, so the relative gain is smaller.

In the same service, setting `response_model=None` on the GET route made no detectable
difference in throughput with `MsgspecRouter` (0.0% [-1.0%, +1.0%]). This is consistent
with an instance of exactly the declared Struct class being encoded without
revalidation.

<details>
<summary>How it was measured</summary>

- Host: a cloud VM with 6 dedicated vCPUs (AMD EPYC-Rome, KVM), Ubuntu 24.04. The API
  ran as one uvicorn worker (h11, asyncio) pinned to one core; PostgreSQL and the load
  generator ran on other cores.
- Software: CPython 3.14.7, FastAPI 0.142.2, Pydantic 2.13.5, msgspec 0.22.0,
  SQLAlchemy 2.1.3 (ORM), uvicorn 0.54.0. The router code on the measured paths was
  the same as msgspec-fastapi 0.0.1.
- Load: a Go generator with 20 closed-loop clients, checking every response body. The
  numbers are throughput at saturation of the single API core. The GET mix sends 9 of
  10 requests to one page and every 10th to a page that is not repeated within the run.
- Each comparison is 8 blocks in ABBA or BAAB order, so each block measures both
  versions twice; every run starts a fresh process with a 30 s warmup and a 60 s
  measurement. The percentages are computed from the mean of the per-block log ratios,
  with a nominal 95% t interval over the 8 blocks. The requests per second values are
  the geometric means of each side, and the CPU values are the arithmetic means.
- CPU time per response is the CPU time of the API container in the measurement
  window divided by the number of checked responses. Because the API core was
  saturated, it is close to the inverse of throughput.
- The benchmark sources are not published yet.

</details>

Things to keep in mind:

- On the control side, FastAPI parses the body with `json.loads` and then validates
  it with Pydantic, which is FastAPI's standard path.
- The GET and search rows also include changes outside the router: database rows are
  converted to msgspec Structs instead of Pydantic models and, for GET, the query model is
  msgspec instead of Pydantic, so they measure a migration, not the router alone.
- Both sides check the same constraints, including custom per-record checks written
  in Python. Results for other schemas and other routes will differ.
- This is one series on one machine. The host slowed down near the end of the series: a
  static control was about 10% slower after the series than before it. The blocks of
  all comparisons ran in 8 rounds, and the slowdown began during the last round, so
  only the last block of each comparison can be affected. Excluding it changes each
  estimate by less than 0.2 percentage points. Compare the relative effects rather than
  the absolute numbers.

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
