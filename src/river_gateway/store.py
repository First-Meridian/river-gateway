"""SQLite persistence: gateway API keys, River accounts (the account pool),
settings and the usage log.

Keys and the active account are looked up in the database on every request
(indexed lookups on small tables), so changes made by the CLI take effect
immediately in a running gateway.

River account API keys must be usable, so they are stored as given: protect
the database file and its backups like the .env file.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KEY_PREFIX = "rgw_"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    allowed_models  TEXT NOT NULL DEFAULT '["*"]',
    rpm             INTEGER,
    max_concurrency INTEGER,
    budget_usd      REAL,
    spent_usd       REAL NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    expires_at      REAL,
    created_at      REAL NOT NULL,
    last_used_at    REAL,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL NOT NULL,
    request_id          TEXT NOT NULL,
    upstream_request_id TEXT,
    key_id              TEXT,
    endpoint            TEXT NOT NULL,
    model               TEXT,
    target_model        TEXT,
    upstream            TEXT,
    stream              INTEGER NOT NULL DEFAULT 0,
    status              INTEGER NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 1,
    prompt_tokens       INTEGER,
    cached_tokens       INTEGER,
    completion_tokens   INTEGER,
    reasoning_tokens    INTEGER,
    cost_usd            REAL,
    latency_ms          INTEGER,
    ttft_ms             INTEGER,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log (ts);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_log (key_id, ts);

-- River accounts the gateway can switch between. source='config' is the key
-- from the config file (api_key stays NULL here and is read from the config).
CREATE TABLE IF NOT EXISTS river_accounts (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'manual',
    api_key          TEXT,
    note             TEXT,
    balance_usd      REAL,
    balance_at       REAL,
    low_balance_at   REAL,
    low_balance_msg  TEXT,
    check_at         REAL,
    check_ok         INTEGER,
    check_status     INTEGER,
    check_models     INTEGER,
    check_error      TEXT,
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_settings (
    name  TEXT PRIMARY KEY,
    value TEXT
);
"""

UPDATABLE_ACCOUNT_FIELDS = {
    "name",
    "api_key",
    "note",
    "balance_usd",
    "balance_at",
    "low_balance_at",
    "low_balance_msg",
    "check_at",
    "check_ok",
    "check_status",
    "check_models",
    "check_error",
}

UPDATABLE_KEY_FIELDS = {
    "name",
    "allowed_models",
    "rpm",
    "max_concurrency",
    "budget_usd",
    "spent_usd",
    "enabled",
    "expires_at",
    "note",
}

_COLUMN_GROUPS = {
    "model": "u.model",
    "target": "u.target_model",
    "upstream": "u.upstream",
    "key": "u.key_id",
    "account": "u.account_id",
}
# Time buckets are computed in a given timezone (seconds east of UTC).
_TIME_GROUPS = {
    "day": "date(u.ts + ?, 'unixepoch')",
    "hour": "strftime('%Y-%m-%d %H:00', u.ts + ?, 'unixepoch')",
}
GROUP_BY_CHOICES = (*_COLUMN_GROUPS, *_TIME_GROUPS)


def local_utc_offset() -> int:
    """This machine's current offset from UTC, in seconds."""
    return time.localtime().tm_gmtoff


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class ApiKey:
    id: str
    name: str
    key_prefix: str
    allowed_models: list[str]
    rpm: int | None
    max_concurrency: int | None
    budget_usd: float | None
    spent_usd: float
    enabled: bool
    expires_at: float | None
    created_at: float
    last_used_at: float | None
    note: str | None

    def allows_model(self, model: str) -> bool:
        return any(fnmatch.fnmatchcase(model, pattern) for pattern in self.allowed_models)

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires_at is not None and (now or time.time()) >= self.expires_at

    def budget_exhausted(self) -> bool:
        return self.budget_usd is not None and self.spent_usd >= self.budget_usd

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageRecord:
    request_id: str
    endpoint: str
    status: int
    key_id: str | None = None
    model: str | None = None
    target_model: str | None = None
    upstream: str | None = None
    stream: bool = False
    attempts: int = 1
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    upstream_request_id: str | None = None
    error: str | None = None
    account_id: str | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class RiverAccount:
    id: str
    name: str
    source: str  # "config" (key from the config file) or "manual"
    note: str | None
    balance_usd: float | None  # balance recorded by an operator from River Console
    balance_at: float | None
    low_balance_at: float | None  # last time River answered 402 for this account
    low_balance_msg: str | None
    check_at: float | None
    check_ok: bool | None
    check_status: int | None
    check_models: int | None
    check_error: str | None
    created_at: float
    api_key: str | None = field(default=None, repr=False)


def _row_to_account(row: sqlite3.Row) -> RiverAccount:
    return RiverAccount(
        id=row["id"],
        name=row["name"],
        source=row["source"],
        note=row["note"],
        balance_usd=row["balance_usd"],
        balance_at=row["balance_at"],
        low_balance_at=row["low_balance_at"],
        low_balance_msg=row["low_balance_msg"],
        check_at=row["check_at"],
        check_ok=None if row["check_ok"] is None else bool(row["check_ok"]),
        check_status=row["check_status"],
        check_models=row["check_models"],
        check_error=row["check_error"],
        created_at=row["created_at"],
        api_key=row["api_key"],
    )


def _row_to_key(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        id=row["id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        allowed_models=json.loads(row["allowed_models"]),
        rpm=row["rpm"],
        max_concurrency=row["max_concurrency"],
        budget_usd=row["budget_usd"],
        spent_usd=row["spent_usd"],
        enabled=bool(row["enabled"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        note=row["note"],
    )


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Bring databases created by earlier versions up to date (idempotent)."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(usage_log)")}
        if "account_id" not in columns:
            self._conn.execute("ALTER TABLE usage_log ADD COLUMN account_id TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_account_ts ON usage_log (account_id, ts)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- keys -------------------------------------------------------------

    def create_key(
        self,
        name: str,
        *,
        allowed_models: list[str] | None = None,
        rpm: int | None = None,
        max_concurrency: int | None = None,
        budget_usd: float | None = None,
        expires_at: float | None = None,
        note: str | None = None,
    ) -> tuple[ApiKey, str]:
        """Create a key and return it with its plaintext secret (shown only once)."""
        secret = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = "key_" + secrets.token_hex(6)
        with self._lock:
            self._conn.execute(
                """INSERT INTO api_keys (id, name, key_hash, key_prefix, allowed_models, rpm,
                       max_concurrency, budget_usd, expires_at, created_at, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key_id,
                    name,
                    hash_secret(secret),
                    secret[: len(KEY_PREFIX) + 6],
                    json.dumps(allowed_models or ["*"]),
                    rpm,
                    max_concurrency,
                    budget_usd,
                    expires_at,
                    time.time(),
                    note,
                ),
            )
        key = self.get_key(key_id)
        assert key is not None
        return key, secret

    def get_key(self, key_id: str) -> ApiKey | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return _row_to_key(row) if row else None

    def find_key(self, secret: str) -> ApiKey | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (hash_secret(secret),)
            ).fetchone()
        return _row_to_key(row) if row else None

    def list_keys(self) -> list[ApiKey]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
        return [_row_to_key(row) for row in rows]

    def update_key(self, key_id: str, **changes: Any) -> ApiKey | None:
        unknown = set(changes) - UPDATABLE_KEY_FIELDS
        if unknown:
            raise ValueError(f"cannot update key fields: {sorted(unknown)}")
        if changes:
            if "allowed_models" in changes:
                changes["allowed_models"] = json.dumps(changes["allowed_models"] or ["*"])
            if "enabled" in changes:
                changes["enabled"] = int(bool(changes["enabled"]))
            assignments = ", ".join(f"{column} = ?" for column in changes)
            with self._lock:
                self._conn.execute(
                    f"UPDATE api_keys SET {assignments} WHERE id = ?", (*changes.values(), key_id)
                )
        return self.get_key(key_id)

    def delete_key(self, key_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        return cursor.rowcount > 0

    def count_keys(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]

    # ---- River accounts and settings ---------------------------------------

    def create_account(
        self,
        name: str,
        *,
        api_key: str | None,
        source: str = "manual",
        note: str | None = None,
        balance_usd: float | None = None,
        account_id: str | None = None,
    ) -> RiverAccount:
        account_id = account_id or "acc_" + secrets.token_hex(5)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO river_accounts (id, name, source, api_key, note, balance_usd, balance_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, name, source, api_key, note, balance_usd, now if balance_usd is not None else None, now),
            )
        account = self.get_account(account_id)
        assert account is not None
        return account

    def get_account(self, account_id: str) -> RiverAccount | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM river_accounts WHERE id = ?", (account_id,)).fetchone()
        return _row_to_account(row) if row else None

    def list_accounts(self) -> list[RiverAccount]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM river_accounts ORDER BY source = 'config' DESC, created_at, name"
            ).fetchall()
        return [_row_to_account(row) for row in rows]

    def update_account(self, account_id: str, **changes: Any) -> RiverAccount | None:
        unknown = set(changes) - UPDATABLE_ACCOUNT_FIELDS
        if unknown:
            raise ValueError(f"cannot update account fields: {sorted(unknown)}")
        if changes:
            if "check_ok" in changes and changes["check_ok"] is not None:
                changes["check_ok"] = int(bool(changes["check_ok"]))
            assignments = ", ".join(f"{column} = ?" for column in changes)
            with self._lock:
                self._conn.execute(
                    f"UPDATE river_accounts SET {assignments} WHERE id = ?", (*changes.values(), account_id)
                )
        return self.get_account(account_id)

    def delete_account(self, account_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM river_accounts WHERE id = ?", (account_id,))
        return cursor.rowcount > 0

    def account_by_setting(self, setting: str, default_id: str) -> RiverAccount | None:
        """The account whose id is stored under `setting` (or `default_id`), in one query."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM river_accounts WHERE id = COALESCE(
                       (SELECT value FROM gateway_settings WHERE name = ?), ?)""",
                (setting, default_id),
            ).fetchone()
        return _row_to_account(row) if row else None

    def account_spend(self) -> dict[str, dict[str, float]]:
        """All-time requests and cost per account."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT account_id, COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS cost_usd
                   FROM usage_log WHERE account_id IS NOT NULL GROUP BY account_id"""
            ).fetchall()
        return {row["account_id"]: {"requests": row["requests"], "cost_usd": row["cost_usd"]} for row in rows}

    def account_spend_since(self, account_id: str, since: float) -> tuple[int, float]:
        """Requests and cost strictly after `since`: a request logged in the same clock tick
        as a recorded balance is already part of that balance (Windows clocks are coarse)."""
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage_log
                   WHERE account_id = ? AND ts > ?""",
                (account_id, since),
            ).fetchone()
        return row[0], row[1]

    def get_setting(self, name: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM gateway_settings WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def set_setting(self, name: str, value: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO gateway_settings (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )

    # ---- usage ------------------------------------------------------------

    def record_usage(self, record: UsageRecord) -> None:
        values = asdict(record)
        values["stream"] = int(record.stream)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    f"INSERT INTO usage_log ({columns}) VALUES ({placeholders})", tuple(values.values())
                )
                if record.key_id:
                    self._conn.execute(
                        "UPDATE api_keys SET spent_usd = spent_usd + ?, last_used_at = ? WHERE id = ?",
                        (record.cost_usd or 0.0, record.ts, record.key_id),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def usage_summary(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        key_id: str | None = None,
        group_by: str = "model",
        utc_offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate usage. Day/hour buckets use `utc_offset` seconds (default: this machine's zone)."""
        if group_by in _TIME_GROUPS:
            group_sql = _TIME_GROUPS[group_by]
            group_params: tuple[Any, ...] = (local_utc_offset() if utc_offset is None else utc_offset,)
        elif group_by in _COLUMN_GROUPS:
            group_sql, group_params = _COLUMN_GROUPS[group_by], ()
        else:
            raise ValueError(f"group_by must be one of {list(GROUP_BY_CHOICES)}")
        where, params = self._usage_filter(since, until, key_id)
        params = (*group_params, *params)
        sql = f"""
            SELECT {group_sql} AS grp,
                   MAX(k.name) AS key_name,
                   MAX(a.name) AS account_name,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN u.status BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS succeeded,
                   COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(u.cached_tokens), 0) AS cached_tokens,
                   COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(u.reasoning_tokens), 0) AS reasoning_tokens,
                   COALESCE(SUM(u.cost_usd), 0) AS cost_usd,
                   CAST(AVG(u.latency_ms) AS INTEGER) AS avg_latency_ms
            FROM usage_log u
            LEFT JOIN api_keys k ON k.id = u.key_id
            LEFT JOIN river_accounts a ON a.id = u.account_id
            {where}
            GROUP BY grp
            ORDER BY cost_usd DESC, requests DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item[group_by] = item.pop("grp")
            if group_by != "key":
                item.pop("key_name")
            if group_by != "account":
                item.pop("account_name")
            result.append(item)
        return result

    def recent_usage(
        self,
        *,
        limit: int = 50,
        key_id: str | None = None,
        before_id: int | None = None,
        errors_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Newest requests first, with the key's name; `before_id` pages backwards."""
        where, params = self._usage_filter(None, None, key_id, before_id=before_id, errors_only=errors_only)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT u.*, k.name AS key_name, a.name AS account_name
                    FROM usage_log u
                    LEFT JOIN api_keys k ON k.id = u.key_id
                    LEFT JOIN river_accounts a ON a.id = u.account_id
                    {where} ORDER BY u.id DESC LIMIT ?""",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _usage_filter(
        since: float | None,
        until: float | None,
        key_id: str | None,
        *,
        before_id: int | None = None,
        errors_only: bool = False,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        params: list[Any] = []
        if before_id is not None:
            clauses.append("u.id < ?")
            params.append(before_id)
        if errors_only:
            clauses.append("(u.status >= 400 OR u.error IS NOT NULL)")
        if since is not None:
            clauses.append("u.ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("u.ts < ?")
            params.append(until)
        if key_id is not None:
            clauses.append("u.key_id = ?")
            params.append(key_id)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)
