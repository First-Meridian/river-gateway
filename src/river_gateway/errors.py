"""OpenAI-style error bodies, matching what River itself returns."""

from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse

ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "upstream_error",
    503: "overloaded_error",
    504: "timeout_error",
}


def error_body(message: str, type_: str, code: str | None = None, param: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}


def error_message(body: bytes) -> str | None:
    """The message of an OpenAI/River-style error body, if that is what it is."""
    try:
        payload = json.loads(body)
    except ValueError:  # includes UnicodeDecodeError
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return None


class GatewayError(Exception):
    """An error produced by the gateway itself (not passed through from River)."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        type_: str | None = None,
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_ or ERROR_TYPES.get(status, "api_error")
        self.code = code
        self.param = param
        self.headers = headers or {}

    def to_response(self, request_id: str | None = None) -> JSONResponse:
        headers = dict(self.headers)
        if request_id:
            headers["x-gateway-request-id"] = request_id
        return JSONResponse(
            error_body(self.message, self.type, self.code, self.param),
            status_code=self.status,
            headers=headers,
        )
