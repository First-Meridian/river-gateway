"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from . import __version__
from .accounts import AccountManager
from .admin import build_admin_router
from .config import GatewayConfig
from .errors import ERROR_TYPES, GatewayError, error_body
from .metrics import Metrics
from .pricing import PricingTable
from .proxy import Gateway
from .ratelimit import RateLimiter
from .routing import Router
from .store import Store
from .upstream import UpstreamPool


def create_app(
    config: GatewayConfig,
    *,
    store: Store | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FastAPI:
    """Build the gateway app. `store`, `transport` and `sleep` are injectable for tests."""
    owns_store = store is None
    store = store if store is not None else Store(config.database)
    metrics = Metrics()
    pricing = PricingTable(config.pricing)
    pool = UpstreamPool(
        config,
        transport=transport,
        sleep=sleep,
        on_retry=lambda upstream, reason: metrics.retries.labels(upstream, reason).inc(),
    )
    gateway = Gateway(config, store, pricing, pool, Router(config), RateLimiter(), metrics, AccountManager(config, store))
    http = httpx.AsyncClient(transport=transport, timeout=20.0)
    started_at = time.time()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        refresher = asyncio.create_task(pricing.run(http)) if config.pricing.url else None
        try:
            yield
        finally:
            if refresher is not None:
                refresher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresher
            await pool.aclose()
            await http.aclose()
            if owns_store:
                store.close()

    app = FastAPI(
        title="River Gateway",
        version=__version__,
        description="OpenAI-compatible gateway for River AI inference.",
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    app.state.store = store
    app.state.pricing = pricing
    app.state.metrics = metrics
    app.state.refresh_pricing = lambda: pricing.refresh(http)

    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["x-request-id", "x-gateway-request-id", "x-gateway-model", "x-gateway-upstream", "retry-after"],
        )

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> Response:
        return exc.to_response()

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        if exc.status_code == 404:
            message = f"Unknown endpoint: {request.method} {request.url.path}"
        return JSONResponse(
            error_body(message, ERROR_TYPES.get(exc.status_code, "invalid_request_error")),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
        errors = exc.errors()
        first: dict[str, Any] = dict(errors[0]) if errors else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = str(first.get("msg", "Invalid request."))
        return JSONResponse(
            error_body(f"{location}: {message}" if location else message, "invalid_request_error", param=location or None),
            status_code=400,
        )

    @app.post("/v1/chat/completions", tags=["openai"])
    async def chat_completions(request: Request) -> Response:
        return await gateway.handle_completion(request, "chat")

    @app.post("/v1/responses", tags=["openai"])
    async def responses(request: Request) -> Response:
        return await gateway.handle_completion(request, "responses")

    @app.get("/v1/models", tags=["openai"])
    async def list_models(request: Request) -> Response:
        return await gateway.list_models(request)

    @app.get("/v1/models/{model_id:path}", tags=["openai"])
    async def retrieve_model(request: Request, model_id: str) -> Response:
        return await gateway.retrieve_model(request, model_id)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    if config.server.metrics:

        @app.get("/metrics", tags=["ops"])
        async def prometheus_metrics() -> Response:
            data, content_type = metrics.render()
            return Response(data, media_type=content_type)

    app.include_router(build_admin_router(gateway, app.state.refresh_pricing, started_at))

    if config.server.ui:
        app.mount("/ui", ConsoleFiles(directory=UI_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def root() -> Response:
            return RedirectResponse("/ui/")

    return app


UI_DIR = Path(__file__).parent / "ui"
# The console is static and talks only to this origin; nothing inline may run.
UI_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-cache",
}


class ConsoleFiles(StaticFiles):
    """Static files for the /ui console, served with security headers."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        for name, value in UI_HEADERS.items():
            response.headers[name] = value
        return response
