from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import PurePath
from typing import Any

import msgspec
from starlette.responses import JSONResponse


def _encode_hook(value: Any) -> str:
    if isinstance(
        value,
        (
            PurePath,
            IPv4Address,
            IPv6Address,
            IPv4Interface,
            IPv6Interface,
            IPv4Network,
            IPv6Network,
        ),
    ):
        return str(value)
    raise TypeError(f'Encoding objects of type {type(value).__name__} is not supported')


_JSON_ENCODER = msgspec.json.Encoder(enc_hook=_encode_hook)


class MsgspecJSONResponse(JSONResponse):
    """Encode JSON with msgspec; use MsgspecRouter for output validation."""

    def render(self, content: Any) -> bytes:
        return _JSON_ENCODER.encode(content)
