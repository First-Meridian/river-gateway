"""Token usage extraction from River responses and server-sent event streams."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

Endpoint = Literal["chat", "responses"]


@dataclass
class Usage:
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _detail(usage: dict[str, Any], key: str, field: str) -> int | None:
    details = usage.get(key)
    return _int(details.get(field)) if isinstance(details, dict) else None


def parse_usage(usage: Any) -> Usage | None:
    """Read a `usage` object from Chat Completions or Responses.

    Chat uses prompt_tokens/completion_tokens; Responses uses
    input_tokens/output_tokens. Cached and reasoning counts are subsets.
    """
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        result = Usage(
            prompt_tokens=_int(usage.get("prompt_tokens")),
            cached_tokens=_detail(usage, "prompt_tokens_details", "cached_tokens"),
            completion_tokens=_int(usage.get("completion_tokens")),
            reasoning_tokens=_detail(usage, "completion_tokens_details", "reasoning_tokens"),
        )
    else:
        result = Usage(
            prompt_tokens=_int(usage.get("input_tokens")),
            cached_tokens=_detail(usage, "input_tokens_details", "cached_tokens"),
            completion_tokens=_int(usage.get("output_tokens")),
            reasoning_tokens=_detail(usage, "output_tokens_details", "reasoning_tokens"),
        )
    if result.prompt_tokens is None and result.completion_tokens is None:
        return None
    return result


def response_id(endpoint: Endpoint, payload: Any) -> str | None:
    """The completion's own id (chatcmpl-... / resp_...) from a body or stream event.

    River's successful responses carry no x-request-id header, so this is
    the identifier the gateway logs for them.
    """
    if not isinstance(payload, dict):
        return None
    if endpoint == "responses" and isinstance(payload.get("response"), dict):
        payload = payload["response"]
    value = payload.get("id")
    return value if isinstance(value, str) and value else None


def summarize_body(endpoint: Endpoint, content: bytes) -> tuple[Usage | None, str | None]:
    """Usage and response id of a non-streaming JSON response."""
    try:
        payload = json.loads(content)
    except ValueError:  # includes UnicodeDecodeError
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return parse_usage(payload.get("usage")), response_id(endpoint, payload)


@dataclass
class SSEEvent:
    raw: bytes
    event: str | None
    data: str | None


class SSEParser:
    """Split a byte stream into complete SSE events, keeping each event's raw bytes."""

    _BOUNDARY = re.compile(rb"\r\n\r\n|\n\n|\r\r")

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._buffer += chunk
        events: list[SSEEvent] = []
        start = 0
        for match in self._BOUNDARY.finditer(self._buffer):
            events.append(self._parse(self._buffer[start : match.end()]))
            start = match.end()
        self._buffer = self._buffer[start:]
        return events

    def flush(self) -> list[SSEEvent]:
        rest, self._buffer = self._buffer, b""
        return [self._parse(rest)] if rest else []

    @staticmethod
    def _parse(raw: bytes) -> SSEEvent:
        event: str | None = None
        data_lines: list[str] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if name == "event":
                event = value
            elif name == "data":
                data_lines.append(value)
        return SSEEvent(raw=raw, event=event, data="\n".join(data_lines) if data_lines else None)


class StreamTracker:
    """Watches a proxied stream for usage, completion, the response id and
    time to first event.

    When the gateway injected `stream_options.include_usage` on the client's
    behalf, the extra usage-only chunk (empty `choices`) is dropped so the
    client sees exactly the stream it asked for.
    """

    _TERMINAL_RESPONSE_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
    _ID_SEARCH_EVENTS = 3  # the id is in the first chat chunk / response.created

    def __init__(self, endpoint: Endpoint, *, drop_usage_chunk: bool = False) -> None:
        self.endpoint = endpoint
        self.drop_usage_chunk = drop_usage_chunk
        self.usage: Usage | None = None
        self.done = False
        self.first_event_at: float | None = None
        self.response_id: str | None = None
        self._id_attempts = 0

    def observe(self, event: SSEEvent) -> bool:
        """Record what the event says; return False if it must not be forwarded."""
        if event.data is None:
            return True
        if self.first_event_at is None:
            self.first_event_at = time.perf_counter()
        data = event.data.strip()
        if data == "[DONE]":
            self.done = True
            return True
        if self.response_id is None and self._id_attempts < self._ID_SEARCH_EVENTS:
            self._id_attempts += 1
            self.response_id = response_id(self.endpoint, _loads(data))
        if self.endpoint == "responses":
            if any(name in data for name in self._TERMINAL_RESPONSE_EVENTS):
                payload = _loads(data)
                if isinstance(payload, dict) and payload.get("type") in self._TERMINAL_RESPONSE_EVENTS:
                    self.done = payload.get("type") != "response.failed"
                    response = payload.get("response")
                    if isinstance(response, dict):
                        self.usage = parse_usage(response.get("usage")) or self.usage
            return True
        if '"usage"' not in data:
            return True
        payload = _loads(data)
        if not isinstance(payload, dict):
            return True
        usage = parse_usage(payload.get("usage"))
        if usage is None:
            return True
        self.usage = usage
        return not (self.drop_usage_chunk and payload.get("choices") == [])


def _loads(data: str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        return None


def stream_error_event(endpoint: Endpoint, message: str) -> bytes:
    """An SSE error event; the OpenAI SDK raises APIError when it sees one."""
    if endpoint == "responses":
        payload = {"type": "error", "code": "upstream_stream_error", "message": message, "param": None}
        return b"event: error\ndata: " + json.dumps(payload).encode() + b"\n\n"
    payload = {"error": {"message": message, "type": "upstream_error", "param": None, "code": "upstream_stream_error"}}
    return b"data: " + json.dumps(payload).encode() + b"\n\n"
