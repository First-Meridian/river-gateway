"""Admin API: manage gateway API keys and read usage. Requires the admin token."""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import WEAK_ADMIN_TOKEN, GatewayConfig, admin_token_is_weak
from .errors import GatewayError
from .store import GROUP_BY_CHOICES, ApiKey, Store

if TYPE_CHECKING:
    from .proxy import Gateway

# Declared as a security scheme so /docs offers an "Authorize" button. Module
# level, because FastAPI resolves the (string) annotation against module globals.
admin_bearer = HTTPBearer(auto_error=False, description="The admin_token from the gateway config.")


class KeyCreate(BaseModel):
    """rpm / max_concurrency: omit for the configured default, 0 for unlimited."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    allowed_models: list[str] | None = None
    rpm: int | None = Field(None, ge=0)
    max_concurrency: int | None = Field(None, ge=0)
    budget_usd: float | None = Field(None, ge=0)
    expires_at: float | None = None
    expires_in_days: float | None = Field(None, gt=0)
    note: str | None = None


class KeyUpdate(BaseModel):
    """Only the fields present are changed; rpm / max_concurrency 0 or null means unlimited."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=200)
    allowed_models: list[str] | None = None
    rpm: int | None = Field(None, ge=0)
    max_concurrency: int | None = Field(None, ge=0)
    budget_usd: float | None = Field(None, ge=0)
    spent_usd: float | None = Field(None, ge=0)
    enabled: bool | None = None
    expires_at: float | None = None
    note: str | None = None


class AccountCreate(BaseModel):
    """A River account: an API key created on the River Console's API Keys page."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=8, max_length=500)
    note: str | None = Field(None, max_length=500)
    balance_usd: float | None = Field(None, ge=0, description="Current balance shown in River Console")
    verify: bool = Field(True, description="Check the key against River (GET /models) before saving")


class AccountUpdate(BaseModel):
    """Only the fields present are changed. Setting balance_usd records it as of now."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=100)
    api_key: str | None = Field(None, min_length=8, max_length=500)
    note: str | None = Field(None, max_length=500)
    balance_usd: float | None = Field(None, ge=0)


def create_key(store: Store, config: GatewayConfig, request: KeyCreate) -> tuple[ApiKey, str]:
    defaults = config.key_defaults
    rpm = defaults.rpm if request.rpm is None else (request.rpm or None)
    concurrency = defaults.max_concurrency if request.max_concurrency is None else (request.max_concurrency or None)
    expires_at = request.expires_at
    if request.expires_in_days is not None:
        expires_at = time.time() + request.expires_in_days * 86400
    return store.create_key(
        request.name,
        allowed_models=request.allowed_models,
        rpm=rpm,
        max_concurrency=concurrency,
        budget_usd=request.budget_usd,
        expires_at=expires_at,
        note=request.note,
    )


def update_key(store: Store, key_id: str, request: KeyUpdate) -> ApiKey | None:
    changes = request.model_dump(exclude_unset=True)
    for name in ("name", "enabled", "spent_usd"):
        if name in changes and changes[name] is None:
            raise GatewayError(400, f"`{name}` cannot be null.", param=name)
    for name in ("rpm", "max_concurrency"):
        if name in changes and not changes[name]:
            changes[name] = None
    return store.update_key(key_id, **changes)


def parse_time(value: str | None) -> float | None:
    """Unix seconds or ISO 8601 (naive values are local time)."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise GatewayError(400, f"Invalid time {value!r}: use unix seconds or ISO 8601.") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def config_warnings(gateway: Gateway) -> list[dict[str, Any]]:
    """Problems worth showing an operator, as stable codes plus an English message."""
    config = gateway.config
    warnings: list[dict[str, Any]] = []
    if config.admin_token and admin_token_is_weak(config.admin_token):
        warnings.append({"code": "weak_admin_token", "message": WEAK_ADMIN_TOKEN})
    accounts = gateway.accounts
    for name, upstream in config.upstreams.items():
        account = accounts.resolve(name)
        if not ((account.api_key if account else None) or upstream.api_key):
            warnings.append(
                {"code": "missing_upstream_key", "upstream": name, "message": f"Upstream {name!r} has no api_key."}
            )
    active = accounts.resolve(accounts.upstream) if accounts.upstream else None
    if active is not None and active.low_balance:
        account = gateway.store.get_account(active.id)
        warnings.append(
            {
                "code": "account_low_balance",
                "account": account.name if account else active.id,
                "message": account.low_balance_msg if account else "HTTP 402",
            }
        )
    if config.pricing.url and gateway.pricing.last_error:
        warnings.append({"code": "pricing_unavailable", "message": gateway.pricing.last_error})
    return warnings


def build_admin_router(
    gateway: Gateway,
    refresh_pricing: Callable[[], Awaitable[bool]],
    started_at: float,
) -> APIRouter:
    config, store, pricing, accounts = gateway.config, gateway.store, gateway.pricing, gateway.accounts

    async def require_admin(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(admin_bearer)]) -> None:
        token = config.admin_token
        if not token:
            raise GatewayError(
                404, "The admin API is disabled. Set `admin_token` in the gateway config.", code="admin_disabled"
            )
        supplied = credentials.credentials.strip() if credentials else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise GatewayError(401, "Invalid admin token.", code="invalid_admin_token")

    router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

    def get_or_404(key_id: str) -> ApiKey:
        key = store.get_key(key_id)
        if key is None:
            raise GatewayError(404, f"No API key with id `{key_id}`.", code="key_not_found")
        return key

    @router.get("/keys")
    async def list_keys() -> dict[str, Any]:
        return {"data": [key.to_public() for key in store.list_keys()]}

    @router.post("/keys", status_code=201)
    async def create(body: KeyCreate) -> dict[str, Any]:
        key, secret = create_key(store, config, body)
        return {"key": key.to_public(), "secret": secret}

    @router.get("/keys/{key_id}")
    async def get(key_id: str) -> dict[str, Any]:
        return get_or_404(key_id).to_public()

    @router.patch("/keys/{key_id}")
    async def patch(key_id: str, body: KeyUpdate) -> dict[str, Any]:
        get_or_404(key_id)
        key = update_key(store, key_id, body)
        assert key is not None
        return key.to_public()

    @router.delete("/keys/{key_id}")
    async def delete(key_id: str) -> dict[str, Any]:
        if not store.delete_key(key_id):
            raise GatewayError(404, f"No API key with id `{key_id}`.", code="key_not_found")
        return {"id": key_id, "deleted": True}

    @router.get("/usage")
    async def usage(
        since: str | None = None,
        until: str | None = None,
        key_id: str | None = None,
        group_by: str = "model",
        tz_offset: int | None = Query(None, ge=-840, le=840, description="Minutes east of UTC for day/hour buckets"),
    ) -> dict[str, Any]:
        if group_by not in GROUP_BY_CHOICES:
            raise GatewayError(400, f"`group_by` must be one of: {', '.join(GROUP_BY_CHOICES)}.", param="group_by")
        since_ts, until_ts = parse_time(since), parse_time(until)
        rows = store.usage_summary(
            since=since_ts,
            until=until_ts,
            key_id=key_id,
            group_by=group_by,
            utc_offset=None if tz_offset is None else tz_offset * 60,
        )
        totals = {
            field: sum(row[field] or 0 for row in rows)
            for field in ("requests", "succeeded", "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "cost_usd")
        }
        return {"group_by": group_by, "since": since_ts, "until": until_ts, "totals": totals, "data": rows}

    @router.get("/usage/recent")
    async def recent(
        limit: int = Query(50, ge=1, le=1000),
        key_id: str | None = None,
        before_id: int | None = Query(None, ge=1),
        errors_only: bool = False,
    ) -> dict[str, Any]:
        rows = store.recent_usage(limit=limit, key_id=key_id, before_id=before_id, errors_only=errors_only)
        return {"data": rows}

    @router.get("/models")
    async def models() -> dict[str, Any]:
        """Every model name clients can use: configured routes (hidden ones included) and upstream models."""
        routes = {route.name: route for route in gateway.router.routes}
        data = []
        for item in await gateway.catalog(include_hidden=True):
            route = routes.get(item["id"])
            data.append(
                {
                    "id": item["id"],
                    "owned_by": item.get("owned_by"),
                    "route": None
                    if route is None
                    else {
                        "upstream": route.upstream or config.default_upstream,
                        "target": route.target_model,
                        "fallbacks": route.fallbacks,
                        "hidden": route.hidden,
                    },
                }
            )
        return {"data": data}

    @router.get("/upstreams/check")
    async def check_upstreams() -> dict[str, Any]:
        """Probe each upstream with the key requests would use (the active account's, where pooled)."""
        names = list(gateway.pool.clients)

        async def probe(name: str) -> dict[str, Any]:
            account = accounts.resolve(name)
            return await gateway.pool.probe(name, account.api_key if account else None)

        results = await asyncio.gather(*(probe(name) for name in names))
        return {"data": dict(zip(names, results, strict=True))}

    # ---- River account pool ------------------------------------------------

    @router.get("/accounts")
    async def list_accounts() -> dict[str, Any]:
        return accounts.describe()

    @router.post("/accounts", status_code=201)
    async def add_account(body: AccountCreate) -> dict[str, Any]:
        account = await accounts.add(
            gateway.pool,
            name=body.name,
            api_key=body.api_key,
            note=body.note,
            balance_usd=body.balance_usd,
            verify=body.verify,
        )
        return accounts.describe(account.id)["data"][0]

    @router.patch("/accounts/{account_id}")
    async def update_account(account_id: str, body: AccountUpdate) -> dict[str, Any]:
        account = accounts.update(account_id, body.model_dump(exclude_unset=True))
        return accounts.describe(account.id)["data"][0]

    @router.delete("/accounts/{account_id}")
    async def delete_account(account_id: str) -> dict[str, Any]:
        accounts.delete(account_id)
        return {"id": account_id, "deleted": True}

    @router.post("/accounts/{account_id}/activate")
    async def activate_account(account_id: str) -> dict[str, Any]:
        accounts.activate(account_id)
        return accounts.describe()

    @router.post("/accounts/{account_id}/check")
    async def check_account(account_id: str) -> dict[str, Any]:
        result = await accounts.check(gateway.pool, account_id)
        return {"result": result, "account": accounts.describe(account_id)["data"][0]}

    @router.get("/pricing")
    async def get_pricing() -> dict[str, Any]:
        return pricing.snapshot()

    @router.post("/pricing/refresh")
    async def refresh() -> dict[str, Any]:
        refreshed = await refresh_pricing()
        return {"refreshed": refreshed, **pricing.snapshot()}

    def active_account() -> dict[str, Any] | None:
        active_id = accounts.active_id()
        account = store.get_account(active_id) if active_id else None
        if account is None:
            return None
        return {"id": account.id, "name": account.name, "low_balance": account.low_balance_at is not None}

    @router.get("/status")
    async def status() -> dict[str, Any]:
        snapshot = pricing.snapshot()
        return {
            "version": __version__,
            "uptime_seconds": int(time.time() - started_at),
            "default_upstream": config.default_upstream,
            "upstreams": {
                name: {
                    "base_url": upstream.base_url,
                    "api_key_set": bool(upstream.api_key),
                    "max_concurrency": upstream.max_concurrency,
                }
                for name, upstream in config.upstreams.items()
            },
            "models": [
                {
                    "name": route.name,
                    "upstream": route.upstream or config.default_upstream,
                    "target": route.target_model,
                    "fallbacks": route.fallbacks,
                }
                for route in config.models
            ],
            "keys": store.count_keys(),
            "account": active_account(),
            "inflight": gateway.limiter.total_inflight(),
            "key_defaults": config.key_defaults.model_dump(),
            "pricing": {
                "models": len(snapshot["models"]),
                "last_refresh": snapshot["last_refresh"],
                "last_error": snapshot["last_error"],
            },
            "warnings": config_warnings(gateway),
        }

    return router
