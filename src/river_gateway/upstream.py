"""HTTP clients for River upstreams, with retries that follow River's guidance.

From docs.river.ai/guides/inference-limits/: back off with jitter on 429/503
and respect Retry-After; retry 500/502/504 with a limit. Retries are new
requests that may be billed again, so the gateway only retries when River
answered with one of those statuses or the request never reached River
(connect/write failures). Once a successful response starts streaming it is
never retried: an interrupted stream cannot be resumed.
"""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import __version__
from .config import GatewayConfig, RetryConfig, UpstreamConfig
from .errors import error_message
from .routing import Target

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
SAFE_TO_RETRY_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)


def _noop() -> None:
    return None


def _once(func: Callable[[], None]) -> Callable[[], None]:
    called = False

    def wrapper() -> None:
        nonlocal called
        if not called:
            called = True
            func()

    return wrapper


def pick_headers(headers: httpx.Headers) -> dict[str, str]:
    """Upstream response headers worth passing to the client."""
    picked = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in ("x-request-id", "retry-after") or lower.startswith("x-ratelimit-"):
            picked[lower] = value
    return picked


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def retry_delay(attempt: int, retry_after: str | None, config: RetryConfig) -> float | None:
    """Seconds to wait before the next attempt, or None if River asked us to
    wait longer than `max_retry_after` (then the caller gives up on this target)."""
    server_delay = parse_retry_after(retry_after)
    if server_delay is not None:
        if server_delay > config.max_retry_after:
            return None
        return server_delay + random.uniform(0, config.backoff_base)
    ceiling = min(config.backoff_max, config.backoff_base * 2 ** (attempt - 1))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


@dataclass
class UpstreamResult:
    """Outcome of sending one request to one target, after retries.

    On success `response` is open with its body unread; the caller must close
    it and call `release` (which frees the upstream concurrency slot).
    """

    target: Target
    attempts: int
    status: int
    response: httpx.Response | None = None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    retryable: bool = False
    injected_usage: bool = False
    release: Callable[[], None] = _noop

    @property
    def ok(self) -> bool:
        return self.response is not None


class UpstreamPool:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_retry: Callable[[str, str], None] | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._on_retry = on_retry or (lambda upstream, reason: None)
        self.clients = {name: self._make_client(upstream, transport) for name, upstream in config.upstreams.items()}
        self._semaphores = {
            name: asyncio.Semaphore(upstream.max_concurrency) if upstream.max_concurrency else None
            for name, upstream in config.upstreams.items()
        }
        self._stream_usage_ok: dict[str, bool] = {}

    @staticmethod
    def _make_client(upstream: UpstreamConfig, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
        headers = {"user-agent": f"river-gateway/{__version__}", **upstream.headers}
        if upstream.api_key:
            headers["authorization"] = f"Bearer {upstream.api_key}"
        return httpx.AsyncClient(
            base_url=upstream.base_url + "/",
            headers=headers,
            timeout=httpx.Timeout(connect=upstream.connect_timeout, read=upstream.read_timeout, write=120.0, pool=None),
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=64),
            transport=transport,
        )

    async def aclose(self) -> None:
        await asyncio.gather(*(client.aclose() for client in self.clients.values()))

    def upstream_config(self, name: str) -> UpstreamConfig:
        return self._config.upstreams[name]

    async def probe(self, name: str, api_key: str | None = None) -> dict[str, Any]:
        """GET /models on an upstream: reachability, key validity and latency (free on River)."""
        headers = {"authorization": f"Bearer {api_key}"} if api_key else None
        started = time.perf_counter()
        try:
            response = await self.clients[name].get("models", headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "status": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "models": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        latency_ms = int((time.perf_counter() - started) * 1000)
        models = None
        if response.is_success:
            try:
                models = len(response.json().get("data", []))
            except (ValueError, AttributeError):
                models = None
        error = None
        if not response.is_success:
            error = error_message(response.content) or response.text[:200] or f"HTTP {response.status_code}"
        return {
            "ok": response.is_success,
            "status": response.status_code,
            "latency_ms": latency_ms,
            "models": models,
            "error": error,
        }

    def stream_usage_supported(self, name: str) -> bool:
        return self._config.upstreams[name].inject_stream_usage and self._stream_usage_ok.get(name, True)

    def mark_stream_usage_unsupported(self, name: str) -> None:
        self._stream_usage_ok[name] = False

    async def _acquire_slot(self, name: str) -> Callable[[], None] | None:
        semaphore = self._semaphores.get(name)
        if semaphore is None:
            return _noop
        try:
            async with asyncio.timeout(self._config.upstreams[name].queue_timeout):
                await semaphore.acquire()
        except TimeoutError:
            return None
        return _once(semaphore.release)

    async def send(
        self, target: Target, path: str, payload: bytes, *, stream: bool, api_key: str | None = None
    ) -> UpstreamResult:
        """Send with retries. `api_key` overrides the upstream's configured key (account pool)."""
        client = self.clients[target.upstream]
        retry = self._config.retry
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        if stream:
            headers.update({"accept": "text/event-stream", "accept-encoding": "identity"})
        attempt = 0
        while True:
            attempt += 1
            release = await self._acquire_slot(target.upstream)
            if release is None:
                return UpstreamResult(
                    target,
                    attempt,
                    status=503,
                    error=f"gateway queue timeout: upstream `{target.upstream}` is at its concurrency limit",
                    retryable=True,
                    headers={"retry-after": "1"},
                )
            request = client.build_request("POST", path, content=payload, headers=headers)
            try:
                response = await client.send(request, stream=True)
            except SAFE_TO_RETRY_ERRORS as exc:
                release()
                if attempt <= retry.max_retries:
                    self._on_retry(target.upstream, "connect_error")
                    await self._sleep(retry_delay(attempt, None, retry) or 0.0)
                    continue
                return UpstreamResult(
                    target, attempt, status=502, error=f"cannot reach upstream: {type(exc).__name__}", retryable=True
                )
            except httpx.TimeoutException as exc:
                release()
                return UpstreamResult(target, attempt, status=504, error=f"upstream timed out: {type(exc).__name__}")
            except httpx.HTTPError as exc:
                release()
                return UpstreamResult(target, attempt, status=502, error=f"upstream error: {type(exc).__name__}: {exc}")

            if response.is_success:
                return UpstreamResult(
                    target,
                    attempt,
                    status=response.status_code,
                    response=response,
                    headers=pick_headers(response.headers),
                    release=release,
                )

            try:
                body = await response.aread()
            except httpx.HTTPError:
                body = b""
            finally:
                await response.aclose()
                release()
            status = response.status_code
            if status in RETRYABLE_STATUS and attempt <= retry.max_retries:
                delay = retry_delay(attempt, response.headers.get("retry-after"), retry)
                if delay is not None:
                    self._on_retry(target.upstream, str(status))
                    await self._sleep(delay)
                    continue
            return UpstreamResult(
                target,
                attempt,
                status=status,
                body=body,
                headers=pick_headers(response.headers),
                error=f"upstream returned HTTP {status}",
                retryable=status in RETRYABLE_STATUS,
            )
