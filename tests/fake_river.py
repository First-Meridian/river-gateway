"""A stand-in for River's OpenAI-compatible inference API (tests and smoke test).

It mirrors what the River docs describe: Bearer auth, OpenAI-style error
bodies, `x-request-id`, Retry-After on 429/503, chat chunks ending with
`data: [DONE]`, Responses streams ending with `response.completed`, and
usage fields including cached and reasoning tokens.

Behaviour is selected by model name:
  fail/503-once  503 + Retry-After: 0 on the first call, then success
  fail/429-long  429 + Retry-After: 120
  fail/500       always 500
  fail/400       always 400
  slow/stream    40 chunks, 0.1 s apart (for disconnect tests)
Names in MODELS succeed; any other model gets a 404.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

API_KEY = "rv_test_key"
MODELS = ["deepseek-ai/DeepSeek-V4.1-Flash", "zai-org/GLM-5.3-Flash", "moonshotai/Kimi-K3"]
SPECIAL_MODELS = {"fail/503-once", "fail/429-long", "fail/500", "fail/400", "slow/stream"}
PRICING = {
    "inference": [
        {"model": "deepseek-ai/DeepSeek-V4.1-Flash", "input": 0.12, "cached_input": 0.0024, "output": 0.48},
        {"model": "zai-org/GLM-5.3-Flash", "input": 0.12, "cached_input": 0.024, "output": 0.4},
        {"model": "moonshotai/Kimi-K3", "input": 3.0, "cached_input": 0.3, "output": 15.0},
    ],
    "training_and_sampling": [],
}
CHAT_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 500,
    "total_tokens": 1500,
    "prompt_tokens_details": {"cached_tokens": 200},
    "completion_tokens_details": {"reasoning_tokens": 100},
}
RESPONSES_USAGE = {
    "input_tokens": 400,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 100,
    "output_tokens_details": {"reasoning_tokens": 20},
    "total_tokens": 500,
}


def sse(payload: Any, event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(payload)}\n\n".encode()


def river_error(status: int, message: str, type_: str = "invalid_request_error", headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        {"error": {"message": message, "type": type_, "code": None}},
        status_code=status,
        headers={"x-request-id": "req_" + uuid.uuid4().hex[:16], **(headers or {})},
    )


class FakeRiver:
    def __init__(self) -> None:
        self.reject_stream_options = False
        # Live River sends no x-request-id on successful responses (checked
        # 2026-10-26); error responses here keep one to test passthrough.
        self.send_request_id = False
        # Echo mode (scripts/dev_server.py): replies repeat the user's message,
        # streams carry reasoning deltas and arrive at a readable pace.
        self.echo = False
        # Keys River accepts, and keys whose account is out of balance (402).
        self.api_keys = {API_KEY}
        self.insufficient_keys: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}
        self.cancelled_streams = 0
        self.app = self._build()

    @staticmethod
    def _key(request: Request) -> str:
        return (request.headers.get("authorization") or "").removeprefix("Bearer ")

    def _ids(self) -> dict[str, str]:
        return {"x-request-id": "req_" + uuid.uuid4().hex[:16]} if self.send_request_id else {}

    def _check(self, request: Request, body: dict[str, Any], endpoint: str) -> Response | None:
        self.requests.append({"endpoint": endpoint, "headers": dict(request.headers), "body": body})
        key = self._key(request)
        if key not in self.api_keys:
            return river_error(401, "Missing or invalid Authorization header. Expected: Bearer <api-key>")
        model = body.get("model")
        count = self.calls[model] = self.calls.get(model, 0) + 1
        if key in self.insufficient_keys:
            return river_error(402, "Insufficient balance. Top up in River Console.", "insufficient_quota")
        if self.reject_stream_options and "stream_options" in body:
            return river_error(400, "Unsupported parameter: stream_options")
        if model == "fail/503-once" and count == 1:
            return river_error(503, "Service overloaded", "overloaded_error", {"retry-after": "0"})
        if model == "fail/429-long":
            return river_error(429, "Too many requests", "rate_limit_error", {"retry-after": "120"})
        if model == "fail/500":
            return river_error(500, "Internal error", "api_error")
        if model == "fail/400":
            return river_error(400, "Unsupported parameter: foo")
        if model not in MODELS and model not in SPECIAL_MODELS:
            return river_error(404, f"Model {model} not found", "not_found_error")
        return None

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> Response:
            body = await request.json()
            if (denied := self._check(request, body, "chat")) is not None:
                return denied
            if body.get("stream"):
                return StreamingResponse(self._chat_stream(body), media_type="text/event-stream", headers=self._ids())
            return JSONResponse(self._chat_completion(body), headers=self._ids())

        @app.post("/v1/responses")
        async def responses(request: Request) -> Response:
            body = await request.json()
            if (denied := self._check(request, body, "responses")) is not None:
                return denied
            if body.get("stream"):
                return StreamingResponse(self._responses_stream(body), media_type="text/event-stream", headers=self._ids())
            return JSONResponse(self._response_object(body, "completed"), headers=self._ids())

        @app.get("/v1/models")
        async def models(request: Request) -> Response:
            if self._key(request) not in self.api_keys:
                return river_error(401, "Missing or invalid Authorization header. Expected: Bearer <api-key>")
            data = [{"id": model, "object": "model", "created": 1760000000, "owned_by": "river"} for model in MODELS]
            return JSONResponse({"object": "list", "data": data})

        @app.get("/api/public/pricing")
        async def pricing() -> Response:
            return JSONResponse(PRICING)

        @app.get("/_fake/stats")
        async def stats() -> Response:
            return JSONResponse(
                {"calls": self.calls, "cancelled_streams": self.cancelled_streams, "requests": len(self.requests)}
            )

        return app

    # ---- chat ---------------------------------------------------------------

    def _reply(self, body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """Content, reasoning and usage of a chat reply."""
        if not self.echo:
            return "Hello from fake River!", "The user wants a greeting.", CHAT_USAGE
        messages = body.get("messages") or []
        last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), {})
        text = last.get("content")
        if isinstance(text, list):
            text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
        text = str(text or "")
        content = f"（模拟 River）你刚才说：{text}"
        reasoning = f"用户发来 {len(text)} 个字符。这是本地模拟服务，所以原样复述。"
        prompt = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict)) + 8
        completion = len(content) + len(reasoning)
        usage = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": len(reasoning)},
        }
        return content, reasoning, usage

    def _chat_completion(self, body: dict[str, Any]) -> dict[str, Any]:
        content, reasoning, usage = self._reply(body)
        message: dict[str, Any] = {"role": "assistant", "content": content, "reasoning_content": reasoning}
        finish_reason = "stop"
        if body.get("tools"):
            name = body["tools"][0]["function"]["name"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": name, "arguments": '{"city": "Paris"}'}}
                ],
            }
            finish_reason = "tool_calls"
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }

    async def _chat_stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        model = body["model"]
        completion_id = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
            return sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                }
            )

        content, reasoning, usage = self._reply(body)
        slow = model == "slow/stream"
        reasoning_pieces: list[str] = []
        delay = 0.0
        if slow:
            pieces, delay = [f"tok{i} " for i in range(40)], 0.1
        elif self.echo:
            reasoning_pieces = [reasoning[i : i + 6] for i in range(0, len(reasoning), 6)]
            pieces, delay = [content[i : i + 3] for i in range(0, len(content), 3)], 0.03
        else:
            pieces = ["Hello", " from", " fake", " River!"]
        completed = False
        try:
            yield chunk({"role": "assistant", "content": ""})
            for piece in reasoning_pieces:
                await asyncio.sleep(delay)
                yield chunk({"reasoning_content": piece})
            for piece in pieces:
                if delay:
                    await asyncio.sleep(delay)
                yield chunk({"content": piece})
            yield chunk({}, "stop")
            if (body.get("stream_options") or {}).get("include_usage"):
                yield sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": usage,
                    }
                )
            yield b"data: [DONE]\n\n"
            completed = True
        finally:
            if not completed:
                self.cancelled_streams += 1

    # ---- responses ----------------------------------------------------------

    @staticmethod
    def _response_object(body: dict[str, Any], status: str) -> dict[str, Any]:
        done = status == "completed"
        output = [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "4", "annotations": []}],
            }
        ]
        return {
            "id": "resp_" + uuid.uuid4().hex[:12],
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": body["model"],
            "output": output if done else [],
            "usage": RESPONSES_USAGE if done else None,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": {},
            "parallel_tool_calls": True,
            "temperature": 1.0,
            "tool_choice": "auto",
            "tools": [],
            "top_p": 1.0,
            "store": False,
        }

    async def _responses_stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        yield sse({"type": "response.created", "sequence_number": 0, "response": self._response_object(body, "in_progress")}, "response.created")
        delta = {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "4",
            "logprobs": [],
        }
        yield sse(delta, "response.output_text.delta")
        completed = {"type": "response.completed", "sequence_number": 2, "response": self._response_object(body, "completed")}
        yield sse(completed, "response.completed")


fake = FakeRiver()
app = fake.app
