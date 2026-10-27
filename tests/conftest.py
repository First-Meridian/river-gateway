from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from river_gateway.app import create_app
from river_gateway.config import GatewayConfig
from river_gateway.store import Store
from tests.fake_river import API_KEY, FakeRiver

ADMIN_TOKEN = "admin-secret"


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    store: Store
    fake: FakeRiver
    sleeps: list[float] = field(default_factory=list)

    def create_key(self, name: str = "test", **kwargs: Any) -> str:
        _, secret = self.store.create_key(name, **kwargs)
        return secret

    async def post(self, path: str, body: Any, key: str | None, **kwargs: Any) -> httpx.Response:
        headers = {"authorization": f"Bearer {key}"} if key else {}
        return await self.client.post(path, json=body, headers=headers, **kwargs)

    async def admin(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"authorization": f"Bearer {ADMIN_TOKEN}"}
        return await self.client.request(method, path, headers=headers, **kwargs)

    def usage_rows(self) -> list[dict[str, Any]]:
        return self.store.recent_usage(limit=100)


def sse_data(text: str) -> list[Any]:
    """Parse the data lines of an SSE body ([DONE] stays a string)."""
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            data = line[len("data: ") :]
            events.append(data if data == "[DONE]" else json.loads(data))
    return events


GatewayFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
def fake_river() -> FakeRiver:
    return FakeRiver()


@pytest.fixture
async def make_gateway(fake_river: FakeRiver) -> AsyncIterator[GatewayFactory]:
    opened: list[tuple[Any, Harness]] = []

    async def factory(transport: httpx.AsyncBaseTransport | None = None, **overrides: Any) -> Harness:
        data: dict[str, Any] = {
            "database": ":memory:",
            "admin_token": ADMIN_TOKEN,
            "upstreams": {"river": {"base_url": "http://river.test/v1", "api_key": API_KEY}},
            "pricing": {"url": "http://river.test/api/public/pricing"},
        }
        data.update(overrides)
        config = GatewayConfig.model_validate(data)
        store = Store(":memory:")
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        app = create_app(
            config,
            store=store,
            transport=transport or httpx.ASGITransport(app=fake_river.app),
            sleep=fake_sleep,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        await app.state.refresh_pricing()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test")
        harness = Harness(app=app, client=client, store=store, fake=fake_river, sleeps=sleeps)
        opened.append((lifespan, harness))
        return harness

    yield factory
    for lifespan, harness in opened:
        await harness.client.aclose()
        await lifespan.__aexit__(None, None, None)
        harness.store.close()
