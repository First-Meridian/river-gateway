"""Model routing: aliases, per-model upstreams (e.g. dedicated deployments) and fallbacks."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig, ModelRoute
from .errors import GatewayError
from .usage import Endpoint


@dataclass(frozen=True)
class Target:
    upstream: str
    model: str
    route: ModelRoute | None = None

    def defaults_for(self, endpoint: Endpoint) -> dict[str, Any]:
        if self.route is None:
            return {}
        return self.route.chat_defaults if endpoint == "chat" else self.route.responses_defaults


class Router:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._routes = {route.name: route for route in config.models}

    @property
    def routes(self) -> list[ModelRoute]:
        return list(self._routes.values())

    def _primary(self, name: str) -> Target | None:
        route = self._routes.get(name)
        if route is not None:
            upstream = route.upstream or self._config.default_upstream
            assert upstream is not None  # guaranteed by config validation
            return Target(upstream=upstream, model=route.target_model, route=route)
        if self._config.default_upstream is not None:
            return Target(upstream=self._config.default_upstream, model=name)
        return None

    def resolve(self, name: str) -> list[Target]:
        """Return the primary target followed by its fallbacks (one level, no duplicates)."""
        primary = self._primary(name)
        if primary is None:
            raise GatewayError(
                404,
                f"The model `{name}` is not configured on this gateway.",
                code="model_not_found",
                param="model",
            )
        targets = [primary]
        seen = {(primary.upstream, primary.model)}
        for fallback in primary.route.fallbacks if primary.route else []:
            target = self._primary(fallback)
            if target is not None and (target.upstream, target.model) not in seen:
                seen.add((target.upstream, target.model))
                targets.append(target)
        return targets


def build_payload(
    body: dict[str, Any], target: Target, endpoint: Endpoint, *, inject_stream_usage: bool
) -> tuple[dict[str, Any], bool]:
    """Copy the client body for one target: rewrite `model`, apply route
    defaults the client did not set, and optionally ask River for streaming usage.

    Returns the payload and whether `stream_options.include_usage` was injected.
    """
    payload = dict(body)
    payload["model"] = target.model
    for key, value in target.defaults_for(endpoint).items():
        payload.setdefault(key, copy.deepcopy(value))
    injected = False
    if inject_stream_usage and endpoint == "chat" and payload.get("stream") is True:
        options = payload.get("stream_options")
        if not isinstance(options, dict) or "include_usage" not in options:
            payload["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
            injected = True
    return payload, injected
