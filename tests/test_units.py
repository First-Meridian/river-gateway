"""Unit tests for parsing, pricing, retries, rate limits, routing and config."""

from __future__ import annotations

import email.utils
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from river_gateway.config import GatewayConfig, ModelRoute, Price, RetryConfig, expand_env, load_config
from river_gateway.pricing import compute_cost
from river_gateway.ratelimit import RateLimiter
from river_gateway.routing import Router, Target, build_payload
from river_gateway.upstream import parse_retry_after, retry_delay
from river_gateway.usage import SSEParser, StreamTracker, Usage, parse_usage


def test_sse_parser_handles_split_chunks_comments_and_crlf() -> None:
    parser = SSEParser()
    stream = b'data: {"a":1}\r\n\r\nda'
    events = parser.feed(stream)
    assert [event.data for event in events] == ['{"a":1}']

    rest = b"ta: [DONE]\n\n: keep-alive\n\nevent: x\ndata: 1\ndata: 2\n\n"
    events = parser.feed(rest)
    assert [(event.event, event.data) for event in events] == [(None, "[DONE]"), (None, None), ("x", "1\n2")]
    assert b"".join(event.raw for event in events) == b"da" + rest

    assert parser.feed(b"data: tail") == []
    assert [event.data for event in parser.flush()] == ["tail"]


def test_parse_usage_reads_both_api_shapes() -> None:
    chat = parse_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
    )
    assert chat == Usage(prompt_tokens=10, cached_tokens=4, completion_tokens=5, reasoning_tokens=2)
    responses = parse_usage({"input_tokens": 7, "output_tokens": 3, "output_tokens_details": {"reasoning_tokens": 1}})
    assert responses == Usage(prompt_tokens=7, cached_tokens=None, completion_tokens=3, reasoning_tokens=1)
    assert parse_usage(None) is None
    assert parse_usage({"total_tokens": 3}) is None


def test_tracker_drops_only_the_injected_usage_chunk() -> None:
    parser = SSEParser()
    events = parser.feed(
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
        b"data: [DONE]\n\n"
    )
    tracker = StreamTracker("chat", drop_usage_chunk=True)
    assert [tracker.observe(event) for event in events] == [True, False, True]
    assert tracker.usage == Usage(prompt_tokens=3, completion_tokens=1) and tracker.done

    keeper = StreamTracker("chat", drop_usage_chunk=False)
    assert all(keeper.observe(event) for event in events)


def test_compute_cost_uses_cached_rate_for_cached_input() -> None:
    usage = Usage(prompt_tokens=1_000_000, cached_tokens=500_000, completion_tokens=1_000_000)
    assert compute_cost(usage, Price(input=1, cached_input=0.1, output=2)) == pytest.approx(0.5 + 0.05 + 2)
    assert compute_cost(usage, Price(input=1, output=2)) == pytest.approx(1 + 2)
    assert compute_cost(usage, None) is None
    assert compute_cost(None, Price()) is None


def test_retry_delay_respects_retry_after_and_backs_off() -> None:
    config = RetryConfig(backoff_base=0.5, backoff_max=8, max_retry_after=30)
    assert retry_delay(1, "120", config) is None
    assert 2 <= retry_delay(1, "2", config) <= 2.5  # type: ignore[operator]
    for attempt in range(1, 8):
        ceiling = min(8, 0.5 * 2 ** (attempt - 1))
        assert ceiling / 2 <= retry_delay(attempt, None, config) <= ceiling  # type: ignore[operator]


def test_parse_retry_after_formats() -> None:
    assert parse_retry_after("3") == 3
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
    later = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 55 <= parse_retry_after(later) <= 61  # type: ignore[operator]


def test_rate_limiter_token_bucket_and_concurrency() -> None:
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert limiter.check_rate("k", 2) == 0 and limiter.check_rate("k", 2) == 0
    assert limiter.check_rate("k", 2) == pytest.approx(30)
    now[0] = 30.0
    assert limiter.check_rate("k", 2) == 0
    assert limiter.check_rate("k", None) == 0

    assert limiter.try_acquire("k", 1) and not limiter.try_acquire("k", 1)
    limiter.release("k")
    assert limiter.try_acquire("k", 1) and limiter.inflight("k") == 1


def test_build_payload_rewrites_model_and_injects_usage() -> None:
    route = ModelRoute(name="fast", target="m", chat_defaults={"temperature": 0.2})
    target = Target("river", "m", route)
    body = {"model": "fast", "stream": True, "messages": [], "stream_options": {"foo": 1}}

    payload, injected = build_payload(body, target, "chat", inject_stream_usage=True)
    assert injected and payload["model"] == "m" and payload["temperature"] == 0.2
    assert payload["stream_options"] == {"foo": 1, "include_usage": True}
    assert body["model"] == "fast" and body["stream_options"] == {"foo": 1}

    explicit = {**body, "stream_options": {"include_usage": False}}
    payload, injected = build_payload(explicit, target, "chat", inject_stream_usage=True)
    assert not injected and payload["stream_options"] == {"include_usage": False}
    _, injected = build_payload(body, target, "responses", inject_stream_usage=True)
    assert not injected


def test_router_resolves_fallbacks_without_duplicates() -> None:
    config = GatewayConfig.model_validate(
        {"models": [{"name": "a", "target": "x", "fallbacks": ["b", "x"]}, {"name": "b", "target": "y"}]}
    )
    router = Router(config)
    assert [(t.upstream, t.model) for t in router.resolve("a")] == [("river", "x"), ("river", "y")]
    assert [t.model for t in router.resolve("anything/else")] == ["anything/else"]


def test_expand_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RG_TEST_VALUE", "value")
    monkeypatch.delenv("RG_TEST_MISSING", raising=False)
    tree = {"a": "${RG_TEST_VALUE}", "b": ["${RG_TEST_MISSING:-fallback}", "x${RG_TEST_MISSING}y"], "c": 3}
    assert expand_env(tree) == {"a": "value", "b": ["fallback", "xy"], "c": 3}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"models": [{"name": "a", "upstream": "nope"}]}, "unknown upstream"),
        ({"models": [{"name": "a", "fallbacks": ["a"]}]}, "itself"),
        ({"models": [{"name": "a"}, {"name": "a"}]}, "duplicate"),
        ({"default_upstream": "nope"}, "default_upstream"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_config_validation_errors(data: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GatewayConfig.model_validate(data)


def test_load_config_reads_dotenv_and_resolves_paths(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "upstreams:\n  river:\n    api_key: ${RG_TEST_DOTENV_KEY}\ndatabase: data/x.db\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text('RG_TEST_DOTENV_KEY="rv_from_dotenv"\n', encoding="utf-8")
    try:
        config = load_config(tmp_path / "config.yaml")
    finally:
        os.environ.pop("RG_TEST_DOTENV_KEY", None)
    assert config.upstreams["river"].api_key == "rv_from_dotenv"
    assert Path(config.database) == tmp_path / "data" / "x.db"
