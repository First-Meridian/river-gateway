"""Command line: run the gateway, manage keys, read usage, check the River connection."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import __version__
from .accounts import AccountManager
from .admin import KeyCreate, KeyUpdate, create_key, update_key
from .backup import (
    SAFETY_PREFIX,
    BackupError,
    DatabaseInfo,
    backup_dir,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
    restore_from_stream,
    stream_backup,
)
from .config import WEAK_ADMIN_TOKEN, GatewayConfig, admin_token_is_weak, load_config
from .errors import GatewayError
from .store import GROUP_BY_CHOICES, ApiKey, Store
from .upstream import UpstreamPool


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _print_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> None:
    if not rows:
        print("(none)")
        return
    cells = [["-" if row.get(key) is None else str(row[key]) for key, _ in columns] for row in rows]
    widths = [max(len(title), *(len(line[i]) for line in cells)) for i, (_, title) in enumerate(columns)]
    print("  ".join(title.ljust(width) for (_, title), width in zip(columns, widths, strict=True)))
    for line in cells:
        print("  ".join(value.ljust(width) for value, width in zip(line, widths, strict=True)))


def _key_row(key: ApiKey) -> dict[str, Any]:
    if not key.enabled:
        status = "disabled"
    elif key.is_expired():
        status = "expired"
    else:
        status = "active"
    budget = f"${key.budget_usd:.2f}" if key.budget_usd is not None else "unlimited"
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.key_prefix + "...",
        "status": status,
        "models": ",".join(key.allowed_models),
        "rpm": key.rpm or "unlimited",
        "concurrency": key.max_concurrency or "unlimited",
        "spend": f"${key.spent_usd:.4f} / {budget}",
        "expires": _fmt_time(key.expires_at),
        "last_used": _fmt_time(key.last_used_at),
    }


KEY_COLUMNS = [
    ("id", "ID"),
    ("name", "NAME"),
    ("prefix", "PREFIX"),
    ("status", "STATUS"),
    ("models", "MODELS"),
    ("rpm", "RPM"),
    ("concurrency", "CONCURRENCY"),
    ("spend", "SPEND / BUDGET"),
    ("expires", "EXPIRES"),
    ("last_used", "LAST USED"),
]


# ---- commands -------------------------------------------------------------


def cmd_serve(config: GatewayConfig, args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    logging.basicConfig(
        level=config.server.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The access log already has one line per request; httpx would add another.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("river_gateway")
    for name, upstream in config.upstreams.items():
        if not upstream.api_key:
            log.warning("Upstream %r has no api_key (set RIVER_API_KEY); River will reject its requests.", name)
    if not config.admin_token:
        log.warning("admin_token is not set, so the /admin API is disabled. Manage keys with the CLI.")
    elif admin_token_is_weak(config.admin_token):
        log.warning(WEAK_ADMIN_TOKEN)
    store = Store(config.database)
    if store.count_keys() == 0:
        log.warning("No gateway API keys exist yet. Create one with: river-gateway keys create --name <name>")
    store.close()
    host = args.host or config.server.host
    port = args.port or config.server.port
    log.info("River Gateway %s on http://%s:%d (database: %s)", __version__, host, port, config.database)
    uvicorn.run(create_app(config), host=host, port=port, log_level=config.server.log_level, access_log=False)
    return 0


def cmd_check(config: GatewayConfig, args: argparse.Namespace) -> int:
    print(f"River Gateway {__version__}")
    print(f"database: {config.database}")
    store = Store(config.database)
    print(f"gateway API keys: {store.count_keys()}")
    store.close()
    if not config.admin_token:
        print("[INFO] admin API disabled (no admin_token)")
    elif admin_token_is_weak(config.admin_token):
        print(f"[WARN] {WEAK_ADMIN_TOKEN}")
    ok = True
    with httpx.Client(timeout=20.0) as client:
        for name, upstream in config.upstreams.items():
            label = f"upstream {name} ({upstream.base_url})"
            is_default = name == config.default_upstream
            if not upstream.api_key:
                print(f"[FAIL] {label}: no api_key configured")
                ok = False
                continue
            try:
                response = client.get(
                    upstream.base_url + "/models",
                    headers={"authorization": f"Bearer {upstream.api_key}", **upstream.headers},
                )
            except httpx.HTTPError as exc:
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
                ok = ok and not is_default
                continue
            if response.is_success:
                try:
                    ids = [item["id"] for item in response.json().get("data", [])]
                except (ValueError, KeyError, TypeError, AttributeError):
                    ids = []
                shown = ", ".join(ids[:8]) + (" ..." if len(ids) > 8 else "")
                print(f"[ OK ] {label}: key accepted, {len(ids)} models: {shown}")
            else:
                print(f"[FAIL] {label}: HTTP {response.status_code} {response.text[:200]}")
                ok = ok and not is_default
        if config.pricing.url:
            try:
                response = client.get(config.pricing.url)
                response.raise_for_status()
                count = len(response.json().get("inference", []))
                print(f"[ OK ] pricing: {count} inference prices from {config.pricing.url}")
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                print(f"[WARN] pricing: {exc} (costs will not be computed)")
    for route in config.models:
        fallbacks = f" (fallbacks: {', '.join(route.fallbacks)})" if route.fallbacks else ""
        print(f"model {route.name} -> {route.target_model} @ {route.upstream or config.default_upstream}{fallbacks}")
    return 0 if ok else 1


def cmd_keys_create(config: GatewayConfig, args: argparse.Namespace) -> int:
    request = KeyCreate(
        name=args.name,
        allowed_models=_split(args.models),
        rpm=args.rpm,
        max_concurrency=args.concurrency,
        budget_usd=args.budget,
        expires_in_days=args.expires_days,
        note=args.note,
    )
    store = Store(config.database)
    try:
        key, secret = create_key(store, config, request)
    finally:
        store.close()
    print(f"Created key {key.id} ({key.name})")
    print(f"  secret: {secret}")
    print("Save the secret now: it is stored hashed and cannot be shown again.")
    return 0


def cmd_keys_list(config: GatewayConfig, args: argparse.Namespace) -> int:
    store = Store(config.database)
    try:
        _print_table([_key_row(key) for key in store.list_keys()], KEY_COLUMNS)
    finally:
        store.close()
    return 0


def _apply_update(config: GatewayConfig, key_id: str, request: KeyUpdate) -> int:
    store = Store(config.database)
    try:
        if store.get_key(key_id) is None:
            print(f"error: no key with id {key_id}", file=sys.stderr)
            return 1
        key = update_key(store, key_id, request)
    finally:
        store.close()
    assert key is not None
    _print_table([_key_row(key)], KEY_COLUMNS)
    return 0


def cmd_keys_update(config: GatewayConfig, args: argparse.Namespace) -> int:
    changes: dict[str, Any] = {}
    if args.name is not None:
        changes["name"] = args.name
    if args.models is not None:
        changes["allowed_models"] = _split(args.models)
    if args.rpm is not None:
        changes["rpm"] = args.rpm
    if args.concurrency is not None:
        changes["max_concurrency"] = args.concurrency
    if args.budget is not None:
        changes["budget_usd"] = args.budget if args.budget >= 0 else None
    if args.reset_spend:
        changes["spent_usd"] = 0.0
    if args.expires_days is not None:
        changes["expires_at"] = time.time() + args.expires_days * 86400 if args.expires_days > 0 else None
    if args.note is not None:
        changes["note"] = args.note
    if not changes:
        print("error: nothing to update", file=sys.stderr)
        return 1
    return _apply_update(config, args.id, KeyUpdate(**changes))


def _set_enabled(enabled: bool) -> Callable[[GatewayConfig, argparse.Namespace], int]:
    def command(config: GatewayConfig, args: argparse.Namespace) -> int:
        return _apply_update(config, args.id, KeyUpdate(enabled=enabled))

    return command


def cmd_keys_delete(config: GatewayConfig, args: argparse.Namespace) -> int:
    store = Store(config.database)
    try:
        deleted = store.delete_key(args.id)
    finally:
        store.close()
    if not deleted:
        print(f"error: no key with id {args.id}", file=sys.stderr)
        return 1
    print(f"Deleted key {args.id}")
    return 0


ACCOUNT_COLUMNS = [
    ("active", "USE"),
    ("id", "ID"),
    ("name", "NAME"),
    ("source", "SOURCE"),
    ("key", "KEY"),
    ("balance", "BALANCE (ESTIMATED)"),
    ("state", "STATE"),
    ("usage", "VIA GATEWAY"),
]


def _account_rows(described: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for account in described["data"]:
        balance = account["balance"]
        if account["low_balance"]:
            state = "out of balance (402)"
        elif account["check"] and account["check"]["ok"] is False:
            state = f"check failed ({account['check']['status'] or 'unreachable'})"
        else:
            state = "ok"
        rows.append(
            {
                "active": "*" if account["active"] else "",
                "id": account["id"],
                "name": account["name"],
                "source": account["source"],
                "key": account["key_hint"] or "(none)",
                "balance": f"${balance['estimated_remaining_usd']:.4f} of ${balance['recorded_usd']:.2f}"
                if balance
                else "not recorded",
                "state": state,
                "usage": f"{account['usage']['requests']} req / ${account['usage']['cost_usd']:.4f}",
            }
        )
    return rows


def _with_accounts(config: GatewayConfig, action: Callable[[AccountManager, UpstreamPool], Any]) -> Any:
    """Run an (async or sync) action with an AccountManager and a temporary upstream pool."""

    async def runner() -> Any:
        store = Store(config.database)
        pool = UpstreamPool(config)
        try:
            result = action(AccountManager(config, store), pool)
            return await result if asyncio.iscoroutine(result) else result
        finally:
            await pool.aclose()
            store.close()

    return asyncio.run(runner())


def cmd_accounts_list(config: GatewayConfig, args: argparse.Namespace) -> int:
    described = _with_accounts(config, lambda manager, pool: manager.describe())
    if described["upstream"] is None:
        print("The account pool is disabled (no default_upstream / accounts.upstream).")
        return 1
    print(f"River accounts for upstream `{described['upstream']}` (* = in use). Balances are estimates.")
    _print_table(_account_rows(described), ACCOUNT_COLUMNS)
    return 0


def cmd_accounts_add(config: GatewayConfig, args: argparse.Namespace) -> int:
    if args.key_env:
        api_key = os.environ.get(args.key_env, "")
        if not api_key:
            print(f"error: environment variable {args.key_env} is empty", file=sys.stderr)
            return 1
    else:
        api_key = getpass.getpass("River API key (input hidden): ")

    async def add(manager: AccountManager, pool: UpstreamPool) -> Any:
        account = await manager.add(
            pool, name=args.name, api_key=api_key, note=args.note, balance_usd=args.balance, verify=not args.no_verify
        )
        if args.use:
            manager.activate(account.id)
        return account

    account = _with_accounts(config, add)
    print(f"Added River account {account.id} ({account.name}){' and switched to it' if args.use else ''}.")
    return 0


def cmd_accounts_use(config: GatewayConfig, args: argparse.Namespace) -> int:
    account = _with_accounts(config, lambda manager, pool: manager.activate(args.id))
    print(f"Requests now use River account {account.id} ({account.name}).")
    return 0


def cmd_accounts_balance(config: GatewayConfig, args: argparse.Namespace) -> int:
    account = _with_accounts(config, lambda manager, pool: manager.update(args.id, {"balance_usd": args.usd}))
    print(f"Recorded a balance of ${args.usd:.2f} for {account.name}; gateway spend from now on is subtracted.")
    return 0


def cmd_accounts_check(config: GatewayConfig, args: argparse.Namespace) -> int:
    async def check(manager: AccountManager, pool: UpstreamPool) -> bool:
        ids = [args.id] if args.id else [a["id"] for a in manager.describe()["data"]]
        all_ok = True
        for account_id in ids:
            result = await manager.check(pool, account_id)
            all_ok = all_ok and result["ok"]
            detail = f"{result['models']} models, {result['latency_ms']} ms" if result["ok"] else result["error"]
            print(f"[{' OK ' if result['ok'] else 'FAIL'}] {account_id}: {detail}")
        return all_ok

    return 0 if _with_accounts(config, check) else 1


def cmd_accounts_remove(config: GatewayConfig, args: argparse.Namespace) -> int:
    _with_accounts(config, lambda manager, pool: manager.delete(args.id))
    print(f"Removed River account {args.id}")
    return 0


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _info(info: DatabaseInfo) -> str:
    return f"integrity ok; rows: {info.summary() or 'none'}"


def cmd_backup(config: GatewayConfig, args: argparse.Namespace) -> int:
    database = config.database
    if args.list:
        files = list_backups(database)
        print(f"Backups in {backup_dir(database)}")
        rows = [
            {
                "file": path.name,
                "kind": "pre-restore copy" if path.name.startswith(SAFETY_PREFIX) else "backup",
                "size": _size(path.stat().st_size),
                "created": _fmt_time(path.stat().st_mtime),
            }
            for path in files
        ]
        _print_table(rows, [("file", "FILE"), ("kind", "KIND"), ("size", "SIZE"), ("created", "CREATED")])
        return 0
    if args.output == "-":
        if sys.stdout.isatty():
            print("error: a backup is binary; redirect it to a file, e.g. `... backup --output - > gateway.db`", file=sys.stderr)
            return 1
        info = stream_backup(database, sys.stdout.buffer)
        print(f"Backup written to stdout ({_info(info)}).", file=sys.stderr)
        return 0
    path, info = create_backup(database, Path(args.output) if args.output else None)
    print(f"Backup written: {path} ({_size(path.stat().st_size)}, {_info(info)})")
    if not args.output and args.keep:
        removed = prune_backups(database, args.keep)
        if removed:
            print(f"Removed {len(removed)} older backup(s); keeping the newest {args.keep}.")
    print("The backup contains River API keys: store it as carefully as your .env file.")
    return 0


def cmd_restore(config: GatewayConfig, args: argparse.Namespace) -> int:
    database = config.database
    from_stdin = args.backup == "-"
    if not args.yes:
        if from_stdin:
            print("error: add --yes when the backup arrives on stdin", file=sys.stderr)
            return 1
        try:
            answer = input(f"Replace {database} with {args.backup}? The current database is saved first. Type yes: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Restore cancelled.")
            return 1
    if from_stdin:
        if sys.stdin.isatty():
            print("error: pipe the backup file into stdin, e.g. `... restore - --yes < gateway.db`", file=sys.stderr)
            return 1
        safety, info = restore_from_stream(database, sys.stdin.buffer)
    else:
        safety, info = restore_backup(database, Path(args.backup))
    print(f"Restored {database} ({_info(info)}).")
    if safety:
        print(f"The previous database was saved as {safety}")
    print("A running gateway uses the restored data from its next request; no restart needed.")
    return 0


def cmd_usage(config: GatewayConfig, args: argparse.Namespace) -> int:
    since = time.time() - args.days * 86400 if args.days else None
    store = Store(config.database)
    try:
        rows = store.usage_summary(since=since, key_id=args.key, group_by=args.by)
    finally:
        store.close()
    columns = [(args.by, args.by.upper())]
    if args.by == "key":
        columns.append(("key_name", "NAME"))
    columns += [
        ("requests", "REQUESTS"),
        ("succeeded", "OK"),
        ("prompt_tokens", "PROMPT"),
        ("cached_tokens", "CACHED"),
        ("completion_tokens", "COMPLETION"),
        ("reasoning_tokens", "REASONING"),
        ("cost", "COST_USD"),
        ("avg_latency_ms", "AVG_MS"),
    ]
    for row in rows:
        row["cost"] = f"{row['cost_usd']:.6f}"
    scope = f"last {args.days:g} days" if args.days else "all time"
    print(f"Usage by {args.by} ({scope}{', key ' + args.key if args.key else ''})")
    _print_table(rows, columns)
    total = sum(row["cost_usd"] for row in rows)
    print(f"Total: {sum(row['requests'] for row in rows)} requests, ${total:.6f}")
    return 0


# ---- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="river-gateway", description="OpenAI-compatible gateway for River AI.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", help="path to config.yaml (default: $RIVER_GATEWAY_CONFIG, then ./config.yaml)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the gateway server")
    serve.add_argument("--host", help="bind address (default: server.host)")
    serve.add_argument("--port", type=int, help="port (default: server.port)")
    serve.set_defaults(func=cmd_serve)

    check = commands.add_parser("check", help="validate config and test the River API key")
    check.set_defaults(func=cmd_check)

    keys = commands.add_parser("keys", help="manage gateway API keys")
    key_commands = keys.add_subparsers(dest="keys_command", required=True)

    create = key_commands.add_parser("create", help="create a key (the secret is printed once)")
    create.add_argument("--name", required=True)
    create.add_argument("--models", help="comma-separated model patterns, e.g. 'deepseek-ai/*,fast' (default: *)")
    create.add_argument("--rpm", type=int, help="requests per minute; 0 = unlimited (default: key_defaults.rpm)")
    create.add_argument("--concurrency", type=int, help="max in-flight requests; 0 = unlimited")
    create.add_argument("--budget", type=float, help="spend limit in USD (default: unlimited)")
    create.add_argument("--expires-days", type=float, help="expire after N days")
    create.add_argument("--note")
    create.set_defaults(func=cmd_keys_create)

    listing = key_commands.add_parser("list", help="list keys")
    listing.set_defaults(func=cmd_keys_list)

    update = key_commands.add_parser("update", help="change a key's limits")
    update.add_argument("id")
    update.add_argument("--name")
    update.add_argument("--models", help="comma-separated model patterns")
    update.add_argument("--rpm", type=int, help="0 = unlimited")
    update.add_argument("--concurrency", type=int, help="0 = unlimited")
    update.add_argument("--budget", type=float, help="USD; negative = unlimited")
    update.add_argument("--reset-spend", action="store_true", help="set spent USD back to 0")
    update.add_argument("--expires-days", type=float, help="expire N days from now; 0 = never")
    update.add_argument("--note")
    update.set_defaults(func=cmd_keys_update)

    for name, enabled in (("enable", True), ("disable", False)):
        toggle = key_commands.add_parser(name, help=f"{name} a key")
        toggle.add_argument("id")
        toggle.set_defaults(func=_set_enabled(enabled))

    delete = key_commands.add_parser("delete", help="delete a key")
    delete.add_argument("id")
    delete.set_defaults(func=cmd_keys_delete)

    accounts = commands.add_parser("accounts", help="manage the River account pool")
    account_commands = accounts.add_subparsers(dest="accounts_command", required=True)
    account_commands.add_parser("list", help="list River accounts and estimated balances").set_defaults(func=cmd_accounts_list)
    add = account_commands.add_parser("add", help="add a River account by its API key (prompted, not echoed)")
    add.add_argument("--name", required=True)
    add.add_argument("--note")
    add.add_argument("--balance", type=float, help="current balance shown in River Console, in USD")
    add.add_argument("--key-env", help="read the API key from this environment variable instead of prompting")
    add.add_argument("--no-verify", action="store_true", help="save without checking the key against River")
    add.add_argument("--use", action="store_true", help="switch to the new account right away")
    add.set_defaults(func=cmd_accounts_add)
    use = account_commands.add_parser("use", help="send requests with this account from now on")
    use.add_argument("id")
    use.set_defaults(func=cmd_accounts_use)
    balance = account_commands.add_parser("balance", help="record the balance shown in River Console")
    balance.add_argument("id")
    balance.add_argument("usd", type=float)
    balance.set_defaults(func=cmd_accounts_balance)
    check_account = account_commands.add_parser("check", help="test account keys against River (free)")
    check_account.add_argument("id", nargs="?")
    check_account.set_defaults(func=cmd_accounts_check)
    remove = account_commands.add_parser("remove", help="remove an account (not the active one)")
    remove.add_argument("id")
    remove.set_defaults(func=cmd_accounts_remove)

    backup = commands.add_parser("backup", help="back up the database while the gateway keeps running")
    backup.add_argument("--output", help="backup file to write, or - for stdout (default: a timestamped file in backups/)")
    backup.add_argument("--keep", type=int, default=14, help="keep the newest N backups in backups/; 0 = keep all (default: 14)")
    backup.add_argument("--list", action="store_true", help="list the backups in backups/")
    backup.set_defaults(func=cmd_backup)

    restore = commands.add_parser("restore", help="replace the database with a backup (the current one is saved first)")
    restore.add_argument("backup", help="backup file, or - to read it from stdin")
    restore.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    restore.set_defaults(func=cmd_restore)

    usage = commands.add_parser("usage", help="summarise recorded usage and cost")
    usage.add_argument("--days", type=float, default=7.0, help="look back N days; 0 = all time (default: 7)")
    usage.add_argument("--by", choices=GROUP_BY_CHOICES, default="model")
    usage.add_argument("--key", help="only this key id")
    usage.set_defaults(func=cmd_usage)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"error: invalid configuration: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(config, args)
    except (ValueError, GatewayError, BackupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
