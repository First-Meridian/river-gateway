"""Prometheus metrics, kept in a per-app registry."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 900)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "river_gateway_requests_total",
            "Completion requests handled by the gateway.",
            ["endpoint", "model", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "river_gateway_request_duration_seconds",
            "Total request duration, including the full stream.",
            ["endpoint", "model"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.first_event = Histogram(
            "river_gateway_time_to_first_event_seconds",
            "Time until the first streamed event reached the gateway.",
            ["endpoint", "model"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.tokens = Counter(
            "river_gateway_tokens_total",
            "Tokens reported by River (kind: prompt, cached, completion, reasoning).",
            ["model", "kind"],
            registry=self.registry,
        )
        self.cost = Counter(
            "river_gateway_cost_usd_total",
            "Estimated cost in USD from River's public prices.",
            ["model"],
            registry=self.registry,
        )
        self.retries = Counter(
            "river_gateway_upstream_retries_total",
            "Upstream retries by reason (HTTP status or connect_error).",
            ["upstream", "reason"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "river_gateway_fallbacks_total",
            "Requests moved from one model to a fallback model.",
            ["from_model", "to_model"],
            registry=self.registry,
        )
        self.inflight = Gauge(
            "river_gateway_inflight_requests",
            "Completion requests currently in flight.",
            ["endpoint"],
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
