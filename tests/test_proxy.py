"""End-to-end tests: client -> gateway -> fake River."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.conftest import GatewayFactory, Harness, sse_data
from tests.fake_river import API_KEY, CHAT_USAGE, PRICING

DEEPSEEK = "deepseek-ai/DeepSeek-V4.1-Flash"
GLM = "zai-org/GLM-5.3-Flash"
CHAT = "/v1/chat/completions"
RESPONSES = "/v1/responses"


def chat_body(model: str = DEEPSEEK, **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 64, **extra}


def image_message(url: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Describe this."}, {"type": "image_url", "image_url": {"url": url}}],
        }
    ]


async def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def key_id(h: Harness) -> str:
    return h.store.list_keys()[0].id


# ---- happy paths ------------------------------------------------------------


async def test_chat_is_proxied_with_the_river_key_and_costed(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(), key)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello from fake River!"
    assert "x-request-id" not in resp.headers
    assert resp.headers["x-gateway-request-id"].startswith("gw-")
    assert resp.headers["x-gateway-upstream"] == "river"
    sent = h.fake.requests[-1]
    assert sent["headers"]["authorization"] == f"Bearer {API_KEY}"
    assert sent["body"]["model"] == DEEPSEEK

    [row] = h.usage_rows()
    assert (row["status"], row["prompt_tokens"], row["cached_tokens"]) == (200, 1000, 200)
    assert (row["completion_tokens"], row["reasoning_tokens"]) == (500, 100)
    assert row["cost_usd"] == pytest.approx((800 * 0.12 + 200 * 0.0024 + 500 * 0.48) / 1e6)
    assert row["upstream_request_id"] == resp.json()["id"]  # no x-request-id, so the completion id
    assert h.store.list_keys()[0].spent_usd == pytest.approx(row["cost_usd"])


async def test_river_x_request_id_is_passed_through_and_logged_when_sent(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    h.fake.send_request_id = True
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(), key)

    assert resp.headers["x-request-id"].startswith("req_")
    assert h.usage_rows()[0]["upstream_request_id"] == resp.headers["x-request-id"]


async def test_stream_gets_usage_without_showing_the_extra_chunk(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(stream=True), key)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert h.fake.requests[-1]["body"]["stream_options"] == {"include_usage": True}
    events = sse_data(resp.text)
    assert events[-1] == "[DONE]"
    assert all(event == "[DONE]" or event["choices"] for event in events)
    text = "".join(e["choices"][0]["delta"].get("content") or "" for e in events if e != "[DONE]")
    assert text == "Hello from fake River!"

    [row] = h.usage_rows()
    assert row["stream"] == 1 and row["completion_tokens"] == 500 and row["error"] is None
    assert row["ttft_ms"] is not None
    assert row["upstream_request_id"] == events[0]["id"]
    assert h.app.state.gateway.limiter.inflight(key_id(h)) == 0


async def test_stream_keeps_the_usage_chunk_when_the_client_asked(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(stream=True, stream_options={"include_usage": True}), key)

    events = sse_data(resp.text)
    assert events[-2]["choices"] == [] and events[-2]["usage"] == CHAT_USAGE


async def test_rejected_stream_options_are_retried_without_and_remembered(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    h.fake.reject_stream_options = True
    key = h.create_key()

    resp = await h.post(CHAT, chat_body(stream=True), key)
    assert resp.status_code == 200 and sse_data(resp.text)[-1] == "[DONE]"
    first, second = h.fake.requests
    assert "stream_options" in first["body"] and "stream_options" not in second["body"]

    await h.post(CHAT, chat_body(stream=True), key)
    assert len(h.fake.requests) == 3 and "stream_options" not in h.fake.requests[-1]["body"]


async def test_responses_defaults_to_store_false_and_records_usage(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    body = {"model": DEEPSEEK, "input": "What is 2 + 2?", "reasoning": {"effort": "high"}}
    resp = await h.post(RESPONSES, body, key)

    assert resp.status_code == 200
    assert resp.json()["output"][0]["content"][0]["text"] == "4"
    assert h.fake.requests[-1]["body"]["store"] is False
    [row] = h.usage_rows()
    assert row["upstream_request_id"] == resp.json()["id"]
    assert (row["prompt_tokens"], row["completion_tokens"], row["reasoning_tokens"]) == (400, 100, 20)
    assert row["cost_usd"] == pytest.approx((400 * 0.12 + 100 * 0.48) / 1e6)


async def test_responses_stream_reads_usage_from_response_completed(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(RESPONSES, {"model": DEEPSEEK, "input": "2 + 2?", "stream": True}, key)

    assert resp.status_code == 200
    assert "event: response.completed" in resp.text
    assert "stream_options" not in h.fake.requests[-1]["body"]
    [row] = h.usage_rows()
    assert row["completion_tokens"] == 100 and row["error"] is None
    assert row["upstream_request_id"].startswith("resp_")  # from response.created


async def test_alias_applies_route_defaults_without_overriding_the_client(make_gateway: GatewayFactory) -> None:
    route = {"name": "fast", "target": DEEPSEEK, "chat_defaults": {"reasoning_effort": "none", "max_tokens": 256}}
    h = await make_gateway(models=[route])
    key = h.create_key()

    await h.post(CHAT, {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}, key)
    sent = h.fake.requests[-1]["body"]
    assert (sent["model"], sent["reasoning_effort"], sent["max_tokens"]) == (DEEPSEEK, "none", 256)

    await h.post(CHAT, {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"}, key)
    assert h.fake.requests[-1]["body"]["reasoning_effort"] == "high"


async def test_routes_a_model_to_a_deployment_upstream(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(
        upstreams={
            "river": {"base_url": "http://river.test/v1", "api_key": API_KEY},
            "deploy": {"base_url": "http://deploy.test/v1", "api_key": API_KEY},
        },
        models=[{"name": "my-agent", "upstream": "deploy", "target": DEEPSEEK, "price": {"input": 0, "output": 0}}],
    )
    key = h.create_key()
    resp = await h.post(CHAT, chat_body("my-agent"), key)

    assert resp.status_code == 200 and resp.headers["x-gateway-upstream"] == "deploy"
    assert h.fake.requests[-1]["headers"]["host"] == "deploy.test"
    [row] = h.usage_rows()
    assert row["upstream"] == "deploy" and row["cost_usd"] == 0


# ---- retries and fallbacks --------------------------------------------------


async def test_503_is_retried_after_retry_after(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(CHAT, chat_body("fail/503-once"), key)

    assert resp.status_code == 200
    assert resp.headers["x-gateway-attempts"] == "2"
    assert h.fake.calls["fail/503-once"] == 2
    assert len(h.sleeps) == 1 and 0 <= h.sleeps[0] <= 0.5
    assert h.usage_rows()[0]["attempts"] == 2


async def test_a_long_retry_after_is_passed_back_not_waited(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(CHAT, chat_body("fail/429-long"), key)

    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "120"
    assert resp.json()["error"]["message"] == "Too many requests"
    assert h.fake.calls["fail/429-long"] == 1 and h.sleeps == []


async def test_server_errors_are_retried_then_fall_back(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(models=[{"name": "smart", "target": "fail/500", "fallbacks": [DEEPSEEK]}])
    key = h.create_key(allowed_models=["smart"])
    resp = await h.post(CHAT, chat_body("smart"), key)

    assert resp.status_code == 200
    assert resp.headers["x-gateway-model"] == DEEPSEEK
    assert resp.headers["x-gateway-attempts"] == "4"
    assert h.fake.calls == {"fail/500": 3, DEEPSEEK: 1}
    [row] = h.usage_rows()
    assert (row["model"], row["target_model"]) == ("smart", DEEPSEEK)


async def test_client_errors_are_not_retried_or_fallen_back(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(models=[{"name": "bad", "target": "fail/400", "fallbacks": [DEEPSEEK]}])
    key = h.create_key()
    resp = await h.post(CHAT, chat_body("bad"), key)

    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "Unsupported parameter: foo"
    assert h.fake.calls == {"fail/400": 1}


async def test_connect_errors_are_retried_then_return_502(make_gateway: GatewayFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pricing"):
            return httpx.Response(200, json=PRICING)
        raise httpx.ConnectError("connection refused", request=request)

    h = await make_gateway(transport=httpx.MockTransport(handler))
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(), key)

    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    assert resp.headers["x-gateway-attempts"] == "3" and len(h.sleeps) == 2


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield (
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":'
            b'[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
        )
        raise httpx.ReadError("connection reset by peer")


async def test_a_broken_stream_ends_with_an_error_event(make_gateway: GatewayFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pricing"):
            return httpx.Response(200, json=PRICING)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())

    h = await make_gateway(transport=httpx.MockTransport(handler))
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(stream=True), key)

    assert resp.status_code == 200
    events = sse_data(resp.text)
    assert events[0]["choices"][0]["delta"]["content"] == "Hel"
    assert events[-1]["error"]["code"] == "upstream_stream_error"
    assert "[DONE]" not in events
    [row] = h.usage_rows()
    assert row["status"] == 502 and "interrupted" in row["error"]
    assert h.app.state.gateway.limiter.inflight(key_id(h)) == 0


async def test_upstream_errors_are_passed_through(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(upstreams={"river": {"base_url": "http://river.test/v1", "api_key": "rv_wrong"}})
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(), key)

    assert resp.status_code == 401
    assert "Authorization" in resp.json()["error"]["message"]
    assert resp.headers["x-request-id"].startswith("req_")
    assert h.usage_rows()[0]["status"] == 401


# ---- access control and limits ------------------------------------------------


async def test_authentication_errors(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    assert (await h.post(CHAT, chat_body(), None)).json()["error"]["code"] == "missing_api_key"
    assert (await h.post(CHAT, chat_body(), "rgw_wrong")).json()["error"]["code"] == "invalid_api_key"
    key = h.create_key()
    h.store.update_key(key_id(h), enabled=False)
    assert (await h.post(CHAT, chat_body(), key)).json()["error"]["code"] == "api_key_disabled"
    h.store.update_key(key_id(h), enabled=True, expires_at=1.0)
    resp = await h.post(CHAT, chat_body(), key)
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "api_key_expired"
    assert h.fake.requests == []


async def test_model_allowlist_and_budget(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key(allowed_models=["deepseek-ai/*"], budget_usd=0.0003)

    denied = await h.post(CHAT, chat_body(GLM), key)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "model_not_allowed"
    assert (await h.post(CHAT, chat_body(), key)).status_code == 200  # spends $0.000336
    over = await h.post(CHAT, chat_body(), key)
    assert over.status_code == 402 and over.json()["error"]["code"] == "budget_exceeded"


async def test_rpm_limit_returns_429_with_retry_after(make_gateway: GatewayFactory) -> None:
    h = await make_gateway()
    key = h.create_key(rpm=1)
    assert (await h.post(CHAT, chat_body(), key)).status_code == 200
    limited = await h.post(CHAT, chat_body(), key)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(limited.headers["retry-after"]) >= 1


def gated_transport(gate: asyncio.Event) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pricing"):
            return httpx.Response(200, json=PRICING)
        await gate.wait()
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "choices": [], "usage": CHAT_USAGE})

    return httpx.MockTransport(handler)


async def test_key_concurrency_limit(make_gateway: GatewayFactory) -> None:
    gate = asyncio.Event()
    h = await make_gateway(transport=gated_transport(gate))
    key = h.create_key(max_concurrency=1)
    limiter = h.app.state.gateway.limiter

    first = asyncio.create_task(h.post(CHAT, chat_body(), key))
    await wait_for(lambda: limiter.inflight(key_id(h)) == 1)
    second = await h.post(CHAT, chat_body(), key)
    assert second.status_code == 429 and second.json()["error"]["code"] == "concurrency_limit_exceeded"
    gate.set()
    assert (await first).status_code == 200
    assert limiter.inflight(key_id(h)) == 0


async def test_upstream_concurrency_queue_times_out_with_503(make_gateway: GatewayFactory) -> None:
    gate = asyncio.Event()
    h = await make_gateway(
        transport=gated_transport(gate),
        upstreams={"river": {"base_url": "http://river.test/v1", "api_key": API_KEY, "max_concurrency": 1, "queue_timeout": 0.05}},
    )
    key = h.create_key()
    limiter = h.app.state.gateway.limiter

    first = asyncio.create_task(h.post(CHAT, chat_body(), key))
    await wait_for(lambda: limiter.inflight(key_id(h)) == 1)
    second = await h.post(CHAT, chat_body(), key)
    assert second.status_code == 503 and second.headers["retry-after"] == "1"
    gate.set()
    assert (await first).status_code == 200


# ---- request validation -------------------------------------------------------


async def test_request_validation(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(server={"max_body_bytes": 2048})
    key = h.create_key()
    auth = {"authorization": f"Bearer {key}"}

    too_big = await h.post(CHAT, chat_body(messages=[{"role": "user", "content": "x" * 4096}]), key)
    assert too_big.status_code == 413
    bad_json = await h.client.post(CHAT, content=b"{not json", headers=auth)
    assert bad_json.status_code == 400 and bad_json.json()["error"]["code"] == "invalid_json"
    no_model = await h.post(CHAT, {"messages": [{"role": "user", "content": "hi"}]}, key)
    assert no_model.status_code == 400 and no_model.json()["error"]["param"] == "model"
    webp = await h.post(CHAT, chat_body(messages=image_message("data:image/webp;base64,AAAA")), key)
    assert webp.status_code == 400 and webp.json()["error"]["code"] == "unsupported_image_type"
    png = await h.post(CHAT, chat_body(messages=image_message("data:image/png;base64,AAAA")), key)
    assert png.status_code == 200
    assert len(h.fake.requests) == 1


@pytest.mark.parametrize(
    ("extra", "param"),
    [
        ({"store": True}, "store"),
        ({"background": True}, "background"),
        ({"previous_response_id": "resp_1"}, "previous_response_id"),
        ({"conversation": "conv_1"}, "conversation"),
    ],
)
async def test_responses_rejects_stateful_features(make_gateway: GatewayFactory, extra: dict[str, Any], param: str) -> None:
    h = await make_gateway()
    key = h.create_key()
    resp = await h.post(RESPONSES, {"model": DEEPSEEK, "input": "hi", **extra}, key)

    assert resp.status_code == 400 and resp.json()["error"]["param"] == param
    assert h.fake.requests == []


async def test_unknown_models_404_without_a_default_upstream(make_gateway: GatewayFactory) -> None:
    h = await make_gateway(default_upstream=None, models=[{"name": "fast", "upstream": "river", "target": DEEPSEEK}])
    key = h.create_key()
    resp = await h.post(CHAT, chat_body(GLM), key)

    assert resp.status_code == 404 and resp.json()["error"]["code"] == "model_not_found"
    assert (await h.post(CHAT, chat_body("fast"), key)).status_code == 200
