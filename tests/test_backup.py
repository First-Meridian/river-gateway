"""Online backup and restore of the SQLite database, and their CLI commands."""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from river_gateway.backup import (
    SAFETY_PREFIX,
    BackupError,
    backup_dir,
    create_backup,
    inspect,
    list_backups,
    prune_backups,
    restore_backup,
)
from river_gateway.cli import main
from river_gateway.store import Store, UsageRecord


@pytest.fixture
def live(tmp_path: Path) -> Store:
    """A database held open the way the running gateway holds it (WAL mode, one connection)."""
    store = Store(str(tmp_path / "gateway.db"))
    store.create_key("team-a")
    store.create_account("team", api_key="rv_team_key_0123456789")
    for i in range(3):
        store.record_usage(UsageRecord(request_id=f"r{i}", endpoint="chat", status=200, cost_usd=0.01))
    yield store
    store.close()


def database_of(store: Store) -> str:
    return store._conn.execute("PRAGMA database_list").fetchone()[2]


def test_backup_while_the_database_is_in_use(live: Store) -> None:
    database = database_of(live)
    path, info = create_backup(database)

    assert path.parent == backup_dir(database) and path.name.startswith("gateway-")
    assert info.ok and info.counts == {"api_keys": 1, "river_accounts": 1, "usage_log": 3}
    assert not Path(str(path) + "-wal").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    # The live database keeps working afterwards.
    live.create_key("team-b")
    assert len(live.list_keys()) == 2


def test_prune_keeps_the_newest_and_never_pre_restore_copies(live: Store) -> None:
    database = database_of(live)
    paths = [create_backup(database)[0] for _ in range(4)]
    for age, path in enumerate(reversed(paths)):  # oldest first gets the oldest mtime
        os.utime(path, (time.time() - 1000 * (age + 1),) * 2)
    safety = backup_dir(database) / f"{SAFETY_PREFIX}20200101-000000.db"
    safety.write_bytes(paths[0].read_bytes())
    os.utime(safety, (time.time() - 10**6,) * 2)

    removed = prune_backups(database, keep=2)
    assert sorted(removed) == sorted(paths[:2])
    assert set(list_backups(database)) == {paths[2], paths[3], safety}


def test_restore_brings_back_old_data_and_saves_the_current_database(live: Store) -> None:
    database = database_of(live)
    backup, _ = create_backup(database)
    live.create_key("added-after-backup")

    safety, info = restore_backup(database, backup)

    assert info.ok and info.counts["api_keys"] == 1
    assert [key.name for key in live.list_keys()] == ["team-a"]  # the open connection sees it at once
    assert safety is not None and safety.name.startswith(SAFETY_PREFIX)
    assert inspect(safety).counts["api_keys"] == 2
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_restore_refuses_files_that_are_not_gateway_databases(live: Store, tmp_path: Path) -> None:
    database = database_of(live)
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"definitely not sqlite" * 100)
    with pytest.raises(BackupError, match="not a usable SQLite database"):
        restore_backup(database, garbage)

    other = tmp_path / "other.db"
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE notes (text TEXT)")
    with pytest.raises(BackupError, match="not a River Gateway database"):
        restore_backup(database, other)
    with pytest.raises(BackupError, match="does not exist"):
        restore_backup(database, tmp_path / "missing.db")
    assert len(live.list_keys()) == 1  # untouched


def test_restoring_a_backup_from_an_older_version_upgrades_its_schema(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:  # the schema before River accounts existed
        conn.executescript(
            """CREATE TABLE api_keys (id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                   key_prefix TEXT NOT NULL, allowed_models TEXT NOT NULL DEFAULT '["*"]', rpm INTEGER,
                   max_concurrency INTEGER, budget_usd REAL, spent_usd REAL NOT NULL DEFAULT 0,
                   enabled INTEGER NOT NULL DEFAULT 1, expires_at REAL, created_at REAL NOT NULL,
                   last_used_at REAL, note TEXT);
               CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                   request_id TEXT NOT NULL, upstream_request_id TEXT, key_id TEXT, endpoint TEXT NOT NULL,
                   model TEXT, target_model TEXT, upstream TEXT, stream INTEGER NOT NULL DEFAULT 0,
                   status INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, prompt_tokens INTEGER,
                   cached_tokens INTEGER, completion_tokens INTEGER, reasoning_tokens INTEGER, cost_usd REAL,
                   latency_ms INTEGER, ttft_ms INTEGER, error TEXT);
               INSERT INTO usage_log (ts, request_id, endpoint, status) VALUES (1, 'old', 'chat', 200);"""
        )
    database = str(tmp_path / "gateway.db")
    _, info = restore_backup(database, old)
    assert {"river_accounts", "gateway_settings"} <= info.tables
    store = Store(database)
    store.record_usage(UsageRecord(request_id="new", endpoint="chat", status=200, account_id="acc_x"))
    assert [row["account_id"] for row in store.recent_usage()] == ["acc_x", None]
    store.close()


# ---- CLI --------------------------------------------------------------------


class FakeStream:
    """Stands in for sys.stdin / sys.stdout when piping a binary backup."""

    def __init__(self, data: bytes = b"") -> None:
        self.buffer = io.BytesIO(data)

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


@pytest.fixture
def config_file(live: Store, tmp_path: Path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(f"database: {database_of(live)}\npricing: {{url: null}}\n", encoding="utf-8")
    return str(path)


def test_cli_backup_list_and_restore(config_file: str, live: Store, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-c", config_file, "backup"]) == 0
    assert "Backup written" in capsys.readouterr().out
    [backup] = list_backups(database_of(live))

    assert main(["-c", config_file, "backup", "--list"]) == 0
    assert backup.name in capsys.readouterr().out

    live.create_key("temporary")
    assert main(["-c", config_file, "restore", str(backup), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Restored" in out and "pre-restore-" in out
    assert [key.name for key in live.list_keys()] == ["team-a"]


def test_cli_restore_asks_for_confirmation(config_file: str, live: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    backup, _ = create_backup(database_of(live))
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["-c", config_file, "restore", str(backup)]) == 1
    assert main(["-c", config_file, "restore", "-"]) == 1  # stdin needs --yes


def test_cli_streams_a_backup_through_stdout_and_back_through_stdin(
    config_file: str, live: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    assert main(["-c", config_file, "backup", "--output", "-"]) == 0
    data = out.buffer.getvalue()
    assert data.startswith(b"SQLite format 3\x00")
    monkeypatch.undo()

    live.create_key("temporary")
    monkeypatch.setattr(sys, "stdin", FakeStream(data))
    assert main(["-c", config_file, "restore", "-", "--yes"]) == 0
    assert [key.name for key in live.list_keys()] == ["team-a"]
