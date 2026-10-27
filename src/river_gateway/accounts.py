"""The River account pool: several River API keys, one of them active.

River has no API for signing in with a password or for reading an account's
balance (docs.river.ai: keys are created on the Console's API Keys page; on a
402, "check your Console balance"). So:

- each account in the pool is an API key created in River Console;
- the active account's key is used for every request to the pool's upstream
  (switching takes effect on the next request, no restart);
- the remaining balance is an estimate: the balance an operator recorded from
  the Console, minus what the gateway has spent through that account since.
  A 402 from River marks the account as out of balance until it succeeds again.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig
from .errors import GatewayError
from .store import RiverAccount, Store
from .upstream import UpstreamPool

CONFIG_ACCOUNT_ID = "acc_config"
CONFIG_ACCOUNT_NAME = "配置文件中的 key"
ACTIVE_SETTING = "active_river_account"


@dataclass(frozen=True)
class ResolvedAccount:
    id: str
    api_key: str | None  # None: send the upstream's configured key
    low_balance: bool


def key_hint(key: str | None) -> str | None:
    """A recognisable but useless fragment of a key, e.g. rv_AbCd…wXyZ."""
    if not key:
        return None
    return f"{key[:7]}…{key[-4:]}" if len(key) > 16 else f"{key[:3]}…"


class AccountManager:
    def __init__(self, config: GatewayConfig, store: Store) -> None:
        self.config = config
        self.store = store
        self.upstream = config.accounts.upstream or config.default_upstream
        if self.upstream and store.get_account(CONFIG_ACCOUNT_ID) is None:
            # Another process (CLI or server) may create it at the same moment.
            with contextlib.suppress(sqlite3.IntegrityError):
                store.create_account(
                    CONFIG_ACCOUNT_NAME,
                    api_key=None,
                    source="config",
                    account_id=CONFIG_ACCOUNT_ID,
                    note="config.yaml / .env 里的 RIVER_API_KEY",
                )

    @property
    def enabled(self) -> bool:
        return self.upstream is not None

    def _require_enabled(self) -> str:
        if self.upstream is None:
            raise GatewayError(
                404,
                "The account pool is disabled: set default_upstream or accounts.upstream in the config.",
                code="accounts_disabled",
            )
        return self.upstream

    def config_key(self) -> str:
        return self.config.upstreams[self.upstream].api_key if self.upstream else ""

    def _get(self, account_id: str) -> RiverAccount:
        account = self.store.get_account(account_id)
        if account is None:
            raise GatewayError(404, f"No River account with id `{account_id}`.", code="account_not_found")
        return account

    # ---- used on every request ---------------------------------------------

    def resolve(self, upstream: str) -> ResolvedAccount | None:
        """The account to use for a request to `upstream` (None if the pool does not cover it)."""
        if upstream != self.upstream:
            return None
        account = self.store.account_by_setting(ACTIVE_SETTING, CONFIG_ACCOUNT_ID)
        if account is None:  # the active account was removed by another process
            account = self.store.get_account(CONFIG_ACCOUNT_ID)
            if account is None:
                return None
        api_key = None if account.source == "config" else account.api_key
        return ResolvedAccount(account.id, api_key, account.low_balance_at is not None)

    def active_id(self) -> str | None:
        resolved = self.resolve(self.upstream) if self.upstream else None
        return resolved.id if resolved else None

    def mark_low_balance(self, account_id: str, message: str | None) -> None:
        self.store.update_account(account_id, low_balance_at=time.time(), low_balance_msg=(message or "HTTP 402")[:300])

    def clear_low_balance(self, account_id: str) -> None:
        self.store.update_account(account_id, low_balance_at=None, low_balance_msg=None)

    # ---- management ----------------------------------------------------------

    def activate(self, account_id: str) -> RiverAccount:
        self._require_enabled()
        account = self._get(account_id)
        if account.source == "config" and not self.config_key():
            raise GatewayError(400, "The config file has no River API key for this upstream.", code="account_has_no_key")
        self.store.set_setting(ACTIVE_SETTING, account_id)
        return account

    async def add(
        self,
        pool: UpstreamPool,
        *,
        name: str,
        api_key: str,
        note: str | None = None,
        balance_usd: float | None = None,
        verify: bool = True,
    ) -> RiverAccount:
        upstream = self._require_enabled()
        api_key = api_key.strip()
        if api_key == self.config_key() or any(a.api_key == api_key for a in self.store.list_accounts()):
            raise GatewayError(409, "This River API key is already in the pool.", code="duplicate_account")
        check = None
        if verify:
            check = await pool.probe(upstream, api_key)
            if check["status"] in (401, 403):
                raise GatewayError(
                    400, f"River rejected this API key (HTTP {check['status']}): {check['error']}", code="invalid_river_key"
                )
            if check["status"] is None:
                raise GatewayError(
                    502, f"Could not reach River to verify the key: {check['error']}", code="upstream_unreachable"
                )
        account = self.store.create_account(name, api_key=api_key, note=note, balance_usd=balance_usd)
        if check is not None:
            self._save_check(account.id, check)
        return self._get(account.id)

    def update(self, account_id: str, changes: dict[str, Any]) -> RiverAccount:
        self._require_enabled()
        account = self._get(account_id)
        for name in ("name", "api_key"):
            if name in changes and not changes[name]:
                raise GatewayError(400, f"`{name}` cannot be empty.", param=name)
        if "api_key" in changes:
            if account.source == "config":
                raise GatewayError(
                    400,
                    "This account's key comes from the config file; edit RIVER_API_KEY in .env instead.",
                    code="config_account_key",
                )
            changes["api_key"] = changes["api_key"].strip()
            # A new key: earlier checks and balance warnings no longer apply.
            changes.update(check_at=None, check_ok=None, check_status=None, check_models=None, check_error=None)
            changes.update(low_balance_at=None, low_balance_msg=None)
        if "balance_usd" in changes:
            changes["balance_at"] = time.time() if changes["balance_usd"] is not None else None
            changes.update(low_balance_at=None, low_balance_msg=None)
        updated = self.store.update_account(account_id, **changes)
        assert updated is not None
        return updated

    def delete(self, account_id: str) -> None:
        self._require_enabled()
        if account_id == CONFIG_ACCOUNT_ID:
            raise GatewayError(400, "The config-file account cannot be removed.", code="config_account")
        self._get(account_id)
        if self.active_id() == account_id:
            raise GatewayError(409, "Switch to another account before removing the active one.", code="account_active")
        self.store.delete_account(account_id)

    async def check(self, pool: UpstreamPool, account_id: str) -> dict[str, Any]:
        upstream = self._require_enabled()
        account = self._get(account_id)
        api_key = None if account.source == "config" else account.api_key
        if account.source == "config" and not self.config_key():
            result = {"ok": False, "status": None, "latency_ms": 0, "models": None, "error": "no API key in the config file"}
        else:
            result = await pool.probe(upstream, api_key)
        self._save_check(account_id, result)
        return result

    def _save_check(self, account_id: str, result: dict[str, Any]) -> None:
        self.store.update_account(
            account_id,
            check_at=time.time(),
            check_ok=result["ok"],
            check_status=result["status"],
            check_models=result["models"],
            check_error=result["error"],
        )

    def describe(self, account_id: str | None = None) -> dict[str, Any]:
        """Accounts with their key hint, balance estimate, warnings and gateway usage."""
        active = self.active_id()
        spend = self.store.account_spend()
        data = []
        for account in self.store.list_accounts():
            if account_id is not None and account.id != account_id:
                continue
            key = self.config_key() if account.source == "config" else account.api_key
            balance = None
            if account.balance_usd is not None and account.balance_at is not None:
                requests, spent = self.store.account_spend_since(account.id, account.balance_at)
                balance = {
                    "recorded_usd": account.balance_usd,
                    "recorded_at": account.balance_at,
                    "requests_since": requests,
                    "spent_since_usd": spent,
                    "estimated_remaining_usd": account.balance_usd - spent,
                }
            data.append(
                {
                    "id": account.id,
                    "name": account.name,
                    "source": account.source,
                    "note": account.note,
                    "key_hint": key_hint(key),
                    "has_key": bool(key),
                    "active": account.id == active,
                    "created_at": account.created_at,
                    "balance": balance,
                    "low_balance": {"at": account.low_balance_at, "message": account.low_balance_msg}
                    if account.low_balance_at
                    else None,
                    "check": {
                        "at": account.check_at,
                        "ok": account.check_ok,
                        "status": account.check_status,
                        "models": account.check_models,
                        "error": account.check_error,
                    }
                    if account.check_at
                    else None,
                    "usage": spend.get(account.id, {"requests": 0, "cost_usd": 0.0}),
                }
            )
        return {"upstream": self.upstream, "active": active, "data": data}
