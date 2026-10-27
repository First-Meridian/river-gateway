"""Token prices from River's public pricing JSON, used to estimate per-request cost."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import Price, PricingConfig
from .usage import Usage

log = logging.getLogger("river_gateway.pricing")

FAILED_REFRESH_RETRY_SECONDS = 300.0


def compute_cost(usage: Usage | None, price: Price | None) -> float | None:
    """USD cost: uncached input at the input rate, cached input at the cached
    rate, and all output (reasoning included) at the output rate."""
    if usage is None or price is None:
        return None
    prompt = usage.prompt_tokens or 0
    cached = min(usage.cached_tokens or 0, prompt)
    completion = usage.completion_tokens or 0
    cached_rate = price.cached_input if price.cached_input is not None else price.input
    return ((prompt - cached) * price.input + cached * cached_rate + completion * price.output) / 1_000_000


class PricingTable:
    def __init__(self, config: PricingConfig) -> None:
        self._config = config
        self._prices: dict[str, Price] = {}
        self.last_refresh: float | None = None
        self.last_error: str | None = None

    def load(self, payload: Any) -> int:
        """Load River's pricing JSON: {"inference": [{"model", "input", "cached_input", "output"}]}."""
        prices: dict[str, Price] = {}
        entries = payload.get("inference") if isinstance(payload, dict) else None
        for entry in entries or []:
            try:
                prices[entry["model"]] = Price(
                    input=float(entry["input"]),
                    cached_input=None if entry.get("cached_input") is None else float(entry["cached_input"]),
                    output=float(entry["output"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._prices = prices
        self.last_refresh = time.time()
        return len(prices)

    async def refresh(self, client: httpx.AsyncClient) -> bool:
        if not self._config.url:
            return False
        try:
            response = await client.get(self._config.url, timeout=20.0)
            response.raise_for_status()
            count = self.load(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Could not refresh River pricing from %s: %s", self._config.url, self.last_error)
            return False
        self.last_error = None
        log.info("Loaded %d River inference prices", count)
        return True

    async def run(self, client: httpx.AsyncClient) -> None:
        while True:
            ok = await self.refresh(client)
            delay = self._config.refresh_seconds
            if not ok:
                delay = min(delay, FAILED_REFRESH_RETRY_SECONDS)
            await asyncio.sleep(delay)

    def price_for(self, model: str, route_price: Price | None = None) -> Price | None:
        if route_price is not None:
            return route_price
        if model in self._config.overrides:
            return self._config.overrides[model]
        return self._prices.get(model)

    def snapshot(self) -> dict[str, Any]:
        merged = {**self._prices, **self._config.overrides}
        return {
            "source": self._config.url,
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
            "unit": "USD per million tokens",
            "models": {name: price.model_dump() for name, price in sorted(merged.items())},
        }
