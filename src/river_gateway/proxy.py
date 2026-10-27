"""The gateway core: authenticate, limit, route, forward and account for each request."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from .config import GatewayConfig
from .errors import ERROR_TYPES, GatewayError, error_body, error_message
from .accounts import AccountManager
from .metrics import Metrics
from .pricing import PricingTable, compute_cost
from .ratelimit import RateLimiter
from .routing import Router, Target, build_payload
from .store import ApiKey, Store, UsageRecord
from .upstream import UpstreamPool, UpstreamResult
from .usage import Endpoint, SSEParser, StreamTracker, Usage, stream_error_event, summarize_body
from .validation import validate_request

log = logging.getLogger("river_gateway")
access_log = logging.getLogger("river_gateway.access")

ENDPOINT_PATHS: dict[Endpoint, str] = {"chat": "chat/completions", "responses": "responses"}


@dataclass
class RequestContext:
    endpoint: Endpoint
    request_id: str = field(default_factory=lambda: "gw-" + uuid.uuid4().hex[:24])
    started: float = field(default_factory=time.perf_counter)
    key: ApiKey | None = None
    model: str | None = None
    stream: bool = False
    target: Target | None = None
    attempts: int = 0
    upstream_request_id: str | None = None
    account_id: str | None = None
    finished: bool = False
    releases: list[Callable[[], None]] = field(default_factory=list)

    def release(self) -> None:
        while self.releases:
            self.releases.pop()()


class ProxyStreamingResponse(StreamingResponse):
    """A StreamingResponse that always runs cleanup, however the stream ends.

    Depending on the ASGI server, a client disconnect either cancels the
    stream or makes `send` fail; in both cases the body generator is closed
    and `on_close` runs, which closes the upstream connection (stopping
    generation on River) and records usage.
    """

    def __init__(self, content: AsyncIterator[bytes], *, on_close: Callable[[], Awaitable[None]], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except (ClientDisconnect, OSError):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                aclose = getattr(self.body_iterator, "aclose", None)
                if aclose is not None:
                    await aclose()
                await self._on_close()


def _encode(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates from \ud8xx escapes
        return json.dumps(payload, separators=(",", ":")).encode("ascii")


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        store: Store,
        pricing: PricingTable,
        pool: UpstreamPool,
        router: Router,
        limiter: RateLimiter,
        metrics: Metrics,
        accounts: AccountManager,
    ) -> None:
        self.config = config
        self.store = store
        self.pricing = pricing
        self.pool = pool
        self.router = router
        self.limiter = limiter
        self.metrics = metrics
        self.accounts = accounts
        # (fetched at, account id the list was fetched with, models)
        self._models_cache: tuple[float, str | None, list[dict[str, Any]]] | None = None
        self._models_lock = asyncio.Lock()

    # ---- authentication ---------------------------------------------------

    def authenticate(self, request: Request) -> ApiKey:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise GatewayError(
                401,
                "Missing or invalid Authorization header. Expected: Bearer <gateway API key>.",
                code="missing_api_key",
            )
        key = self.store.find_key(token)
        if key is None:
            raise GatewayError(401, "Invalid API key.", code="invalid_api_key")
        if not key.enabled:
            raise GatewayError(401, "This API key is disabled.", code="api_key_disabled")
        if key.is_expired():
            raise GatewayError(401, "This API key has expired.", code="api_key_expired")
        return key

    # ---- chat completions and responses -----------------------------------

    async def handle_completion(self, request: Request, endpoint: Endpoint) -> Response:
        ctx = RequestContext(endpoint=endpoint)
        try:
            return await self._handle(request, ctx)
        except GatewayError as exc:
            self._finish(ctx, status=exc.status, error=exc.message)
            return exc.to_response(ctx.request_id)
        except asyncio.CancelledError:
            self._finish(ctx, status=499, error="client disconnected")
            raise
        except Exception:
            log.exception("Unhandled error in request %s", ctx.request_id)
            self._finish(ctx, status=500, error="internal gateway error")
            return GatewayError(500, "Internal gateway error.").to_response(ctx.request_id)

    async def _handle(self, request: Request, ctx: RequestContext) -> Response:
        key = self.authenticate(request)
        ctx.key = key
        body = await self._read_json(request)
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise GatewayError(400, "`model` is required.", param="model")
        ctx.model = model
        if not key.allows_model(model):
            raise GatewayError(
                403, f"This API key is not allowed to use model `{model}`.", code="model_not_allowed", param="model"
            )
        if key.budget_exhausted():
            raise GatewayError(
                402, f"This API key has used its budget of ${key.budget_usd:.2f}.", code="budget_exceeded"
            )
        validate_request(ctx.endpoint, body, self.config.validation)
        ctx.stream = body.get("stream") is True
        targets = self.router.resolve(model)
        self._acquire_key_slot(ctx, key)
        return await self._forward(ctx, body, targets)

    async def _read_json(self, request: Request) -> dict[str, Any]:
        limit = self.config.server.max_body_bytes
        too_large = GatewayError(
            413,
            f"Request body exceeds the gateway limit of {limit} bytes "
            "(River accepts at most 50 MiB of JSON, including base64 images).",
            code="request_too_large",
        )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise too_large
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise too_large
                chunks.append(chunk)
        except ClientDisconnect:
            raise GatewayError(400, "Client disconnected while sending the request.") from None
        try:
            body = json.loads(b"".join(chunks))
        except (ValueError, UnicodeDecodeError):
            raise GatewayError(400, "Request body must be valid JSON.", code="invalid_json") from None
        if not isinstance(body, dict):
            raise GatewayError(400, "Request body must be a JSON object.", code="invalid_json")
        return body

    def _acquire_key_slot(self, ctx: RequestContext, key: ApiKey) -> None:
        if not self.limiter.try_acquire(key.id, key.max_concurrency):
            raise GatewayError(
                429,
                f"Too many concurrent requests for this API key (limit {key.max_concurrency}).",
                code="concurrency_limit_exceeded",
                headers={"retry-after": "1"},
            )
        wait = self.limiter.check_rate(key.id, key.rpm)
        if wait > 0:
            self.limiter.release(key.id)
            raise GatewayError(
                429,
                f"Rate limit exceeded for this API key ({key.rpm} requests per minute).",
                code="rate_limit_exceeded",
                headers={"retry-after": str(max(1, math.ceil(wait)))},
            )
        gauge = self.metrics.inflight.labels(ctx.endpoint)
        gauge.inc()
        ctx.releases.append(lambda: self.limiter.release(key.id))
        ctx.releases.append(gauge.dec)

    async def _forward(self, ctx: RequestContext, body: dict[str, Any], targets: list[Target]) -> Response:
        path = ENDPOINT_PATHS[ctx.endpoint]
        result: UpstreamResult | None = None
        for index, target in enumerate(targets):
            if index > 0 and result is not None:
                self.metrics.fallbacks.labels(result.target.model, target.model).inc()
                log.warning(
                    "%s: falling back from %s to %s (%s)",
                    ctx.request_id,
                    result.target.model,
                    target.model,
                    result.error,
                )
            result = await self._send(ctx, body, target, path)
            ctx.attempts += result.attempts
            ctx.target = target
            if result.ok:
                return await self._success(ctx, result)
            if not result.retryable:
                break
        assert result is not None
        return self._failure(ctx, result)

    async def _send(self, ctx: RequestContext, body: dict[str, Any], target: Target, path: str) -> UpstreamResult:
        inject = ctx.stream and self.pool.stream_usage_supported(target.upstream)
        payload, injected = build_payload(body, target, ctx.endpoint, inject_stream_usage=inject)
        account = self.accounts.resolve(target.upstream)
        ctx.account_id = account.id if account else None
        api_key = account.api_key if account else None
        result = await self.pool.send(target, path, _encode(payload), stream=ctx.stream, api_key=api_key)
        if injected and result.status in (400, 422) and b"stream_options" in result.body:
            # River rejected the usage option we added; stop adding it for this upstream.
            log.warning("Upstream %s rejected stream_options; not requesting streaming usage again", target.upstream)
            self.pool.mark_stream_usage_unsupported(target.upstream)
            payload, injected = build_payload(body, target, ctx.endpoint, inject_stream_usage=False)
            retried = await self.pool.send(target, path, _encode(payload), stream=ctx.stream, api_key=api_key)
            retried.attempts += result.attempts
            result = retried
        result.injected_usage = injected
        if account is not None:
            if result.status == 402:
                self.accounts.mark_low_balance(account.id, error_message(result.body) or result.error)
            elif result.ok and account.low_balance:
                self.accounts.clear_low_balance(account.id)
        return result

    def _headers(self, ctx: RequestContext, result: UpstreamResult) -> dict[str, str]:
        headers = dict(result.headers)
        headers["x-gateway-request-id"] = ctx.request_id
        headers["x-gateway-upstream"] = result.target.upstream
        headers["x-gateway-model"] = result.target.model
        headers["x-gateway-attempts"] = str(ctx.attempts)
        if ctx.stream and result.ok:
            headers["cache-control"] = "no-cache"
            headers["x-accel-buffering"] = "no"
        return headers

    async def _success(self, ctx: RequestContext, result: UpstreamResult) -> Response:
        response = result.response
        assert response is not None
        ctx.upstream_request_id = result.headers.get("x-request-id")
        headers = self._headers(ctx, result)
        if ctx.stream:
            return self._stream(ctx, result, headers)
        try:
            content = await response.aread()
        except httpx.HTTPError as exc:
            raise GatewayError(
                502, f"Upstream connection failed while reading the response ({type(exc).__name__}).", code="upstream_error"
            ) from exc
        finally:
            await response.aclose()
            result.release()
        usage, completion_id = summarize_body(ctx.endpoint, content)
        ctx.upstream_request_id = ctx.upstream_request_id or completion_id
        self._finish(ctx, status=response.status_code, usage=usage)
        return Response(
            content,
            status_code=response.status_code,
            headers=headers,
            media_type=response.headers.get("content-type", "application/json"),
        )

    def _stream(self, ctx: RequestContext, result: UpstreamResult, headers: dict[str, str]) -> Response:
        response = result.response
        assert response is not None
        tracker = StreamTracker(ctx.endpoint, drop_usage_chunk=result.injected_usage)
        closed = False

        async def close(status: int, error: str | None) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            with anyio.CancelScope(shield=True):
                await response.aclose()
            result.release()
            ctx.upstream_request_id = ctx.upstream_request_id or tracker.response_id
            self._finish(ctx, status=status, usage=tracker.usage, error=error, first_event_at=tracker.first_event_at)

        async def body() -> AsyncIterator[bytes]:
            parser = SSEParser()
            status, error = 499, "client disconnected"
            try:
                async for chunk in response.aiter_bytes():
                    for event in parser.feed(chunk):
                        if tracker.observe(event):
                            yield event.raw
                for event in parser.flush():
                    if tracker.observe(event):
                        yield event.raw
                status = response.status_code
                error = None if tracker.done else "stream ended without a completion event"
            except httpx.HTTPError as exc:
                status, error = 502, f"upstream stream interrupted ({type(exc).__name__})"
                log.warning("%s: %s", ctx.request_id, error)
                yield stream_error_event(
                    ctx.endpoint,
                    "The upstream stream was interrupted and the response is incomplete. "
                    "Streams cannot be resumed; retrying sends a new (billed) request.",
                )
            finally:
                await close(status, error)

        return ProxyStreamingResponse(
            body(),
            on_close=lambda: close(499, "client disconnected"),
            status_code=response.status_code,
            headers=headers,
            media_type=response.headers.get("content-type", "text/event-stream"),
        )

    def _failure(self, ctx: RequestContext, result: UpstreamResult) -> Response:
        status = result.status or 502
        ctx.upstream_request_id = result.headers.get("x-request-id")
        headers = self._headers(ctx, result)
        body = result.body
        message = error_message(body)
        if message is None:
            snippet = body[:200].decode("utf-8", errors="replace").strip()
            message = result.error or f"upstream returned HTTP {status}"
            if snippet:
                message = f"{message}: {snippet}"
            body = json.dumps(error_body(message, ERROR_TYPES.get(status, "upstream_error"), code="upstream_error")).encode()
        self._finish(ctx, status=status, error=message[:500])
        return Response(body, status_code=status, headers=headers, media_type="application/json")

    # ---- accounting -------------------------------------------------------

    def _finish(
        self,
        ctx: RequestContext,
        *,
        status: int,
        usage: Usage | None = None,
        error: str | None = None,
        first_event_at: float | None = None,
    ) -> None:
        if ctx.finished:
            return
        ctx.finished = True
        ctx.release()
        elapsed = time.perf_counter() - ctx.started
        target = ctx.target
        cost = None
        if usage is not None and target is not None:
            price = self.pricing.price_for(target.model, target.route.price if target.route else None)
            cost = compute_cost(usage, price)
        ttft_ms = int((first_event_at - ctx.started) * 1000) if first_event_at else None
        if ctx.key is not None and target is not None:
            record = UsageRecord(
                request_id=ctx.request_id,
                endpoint=ctx.endpoint,
                status=status,
                key_id=ctx.key.id,
                model=ctx.model,
                target_model=target.model,
                upstream=target.upstream,
                stream=ctx.stream,
                attempts=ctx.attempts,
                prompt_tokens=usage.prompt_tokens if usage else None,
                cached_tokens=usage.cached_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                reasoning_tokens=usage.reasoning_tokens if usage else None,
                cost_usd=cost,
                latency_ms=int(elapsed * 1000),
                ttft_ms=ttft_ms,
                upstream_request_id=ctx.upstream_request_id,
                error=error,
                account_id=ctx.account_id,
            )
            try:
                self.store.record_usage(record)
            except sqlite3.Error:
                log.exception("Failed to record usage for %s", ctx.request_id)
        self._observe(ctx, status, elapsed, usage, cost, first_event_at)
        access_log.info(
            "%s key=%s model=%s target=%s status=%s stream=%d attempts=%d tokens=%s cost=%s latency=%dms%s%s%s",
            ctx.request_id,
            ctx.key.name if ctx.key else "-",
            ctx.model or "-",
            f"{target.model}@{target.upstream}" if target else "-",
            status,
            int(ctx.stream),
            ctx.attempts,
            f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage else "-",
            f"${cost:.6f}" if cost is not None else "-",
            int(elapsed * 1000),
            f" ttft={ttft_ms}ms" if ttft_ms is not None else "",
            f" upstream_request_id={ctx.upstream_request_id}" if ctx.upstream_request_id else "",
            f" error={error!r}" if error else "",
        )

    def _observe(
        self,
        ctx: RequestContext,
        status: int,
        elapsed: float,
        usage: Usage | None,
        cost: float | None,
        first_event_at: float | None,
    ) -> None:
        model = ctx.target.model if ctx.target else (ctx.model or "-")
        self.metrics.requests.labels(ctx.endpoint, model, str(status)).inc()
        if ctx.target is None:
            return
        self.metrics.latency.labels(ctx.endpoint, model).observe(elapsed)
        if first_event_at is not None:
            self.metrics.first_event.labels(ctx.endpoint, model).observe(first_event_at - ctx.started)
        if usage is not None:
            for kind, value in (
                ("prompt", usage.prompt_tokens),
                ("cached", usage.cached_tokens),
                ("completion", usage.completion_tokens),
                ("reasoning", usage.reasoning_tokens),
            ):
                if value:
                    self.metrics.tokens.labels(model, kind).inc(value)
        if cost:
            self.metrics.cost.labels(model).inc(cost)

    # ---- models -----------------------------------------------------------

    async def list_models(self, request: Request) -> Response:
        try:
            key = self.authenticate(request)
        except GatewayError as exc:
            return exc.to_response()
        catalog = await self.catalog()
        return JSONResponse({"object": "list", "data": [m for m in catalog if key.allows_model(m["id"])]})

    async def retrieve_model(self, request: Request, model_id: str) -> Response:
        try:
            key = self.authenticate(request)
        except GatewayError as exc:
            return exc.to_response()
        if key.allows_model(model_id):
            for model in await self.catalog():
                if model["id"] == model_id:
                    return JSONResponse(model)
        return GatewayError(
            404, f"The model `{model_id}` does not exist.", code="model_not_found", param="model"
        ).to_response()

    async def catalog(self, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for route in self.router.routes:
            if include_hidden or not route.hidden:
                entries[route.name] = {"id": route.name, "object": "model", "created": 0, "owned_by": "river-gateway"}
        if self.config.expose_upstream_models and self.config.default_upstream:
            for item in await self._upstream_models():
                entries.setdefault(item["id"], item)
        return list(entries.values())

    async def _upstream_models(self) -> list[dict[str, Any]]:
        upstream = self.config.default_upstream
        assert upstream is not None
        # Accounts can differ in model access, so the list is cached per account.
        account = self.accounts.resolve(upstream)
        account_id = account.id if account else None
        headers = {"authorization": f"Bearer {account.api_key}"} if account and account.api_key else None
        ttl = self.config.models_cache_seconds

        def fresh(entry: tuple[float, str | None, list[dict[str, Any]]] | None) -> bool:
            return entry is not None and entry[1] == account_id and time.monotonic() - entry[0] < ttl

        if fresh(self._models_cache):
            return self._models_cache[2]  # type: ignore[index]
        async with self._models_lock:
            cached = self._models_cache
            if fresh(cached):
                return cached[2]  # type: ignore[index]
            client = self.pool.clients[upstream]
            try:
                response = await client.get("models", headers=headers, timeout=15.0)
                response.raise_for_status()
                data = response.json().get("data", [])
                models = [m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                log.warning("Could not list models from upstream %s: %s", upstream, exc)
                stale = cached[2] if cached and cached[1] == account_id else []
                # Serve what we have and try again in at most 30 seconds.
                self._models_cache = (time.monotonic() - ttl + min(ttl, 30.0), account_id, stale)
                return stale
            self._models_cache = (time.monotonic(), account_id, models)
            return models
