from typing import Any
import msgspec
from starlette.responses import JSONResponse

_JSON_ENCODER = msgspec.json.Encoder()

class MsgspecJSONResponse(JSONResponse):
    """Encode JSON with msgspec; use MsgspecRouter for output validation."""
    def render(self, content: Any) -> bytes:
        return _JSON_ENCODER.encode(content)
