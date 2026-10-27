"""Online backup and restore of the gateway's SQLite database.

SQLite's backup API copies a consistent snapshot while the gateway keeps
serving (in WAL mode readers never block writers), so a backup needs no
downtime. Restores go through the same API, so a running gateway sees the
restored data on its next request. Every backup is integrity-checked before
it is kept, and a restore first saves the current database as pre-restore-*.

Backups hold River account API keys and the usage history: keep them as
carefully as the .env file.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from .store import Store

BACKUP_PREFIX = "gateway-"
SAFETY_PREFIX = "pre-restore-"
REQUIRED_TABLES = {"api_keys", "usage_log"}
COUNTED_TABLES = ("api_keys", "river_accounts", "usage_log")


class BackupError(Exception):
    pass


@dataclass
class DatabaseInfo:
    ok: bool
    tables: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def summary(self) -> str:
        return ", ".join(f"{table} {count}" for table, count in self.counts.items())


def backup_dir(database: str) -> Path:
    """Where `backup` keeps its files by default: a backups/ folder next to the database."""
    return Path(database).resolve().parent / "backups"


def inspect(path: Path) -> DatabaseInfo:
    """Integrity check, tables and row counts of a database file (nothing is written)."""
    if not path.is_file():
        return DatabaseInfo(False, error=f"{path} does not exist")
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in COUNTED_TABLES if t in tables}
    except sqlite3.DatabaseError as exc:
        return DatabaseInfo(False, error=str(exc))
    finally:
        conn.close()
    return DatabaseInfo(result == "ok", tables, counts, None if result == "ok" else str(result))


def _unique_path(directory: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{prefix}{stamp}.db"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{prefix}{stamp}-{counter}.db"
        counter += 1
    return candidate


def _snapshot(source: Path, target: Path) -> None:
    """Copy a consistent snapshot of `source` into the new file `target`."""
    src = sqlite3.connect(source, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        # A standalone file: no -wal/-shm companions next to the backup.
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()


def create_backup(database: str, output: Path | None = None) -> tuple[Path, DatabaseInfo]:
    """Back up `database` to `output` (default: a timestamped file in backup_dir)."""
    source = Path(database)
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    target = output or _unique_path(backup_dir(database), BACKUP_PREFIX)
    if target.exists():
        raise BackupError(f"{target} already exists; choose another --output")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        _snapshot(source, partial)
        info = inspect(partial)
        if not info.ok:
            raise BackupError(f"the backup failed its integrity check: {info.error}")
        if os.name == "posix":
            os.chmod(partial, 0o600)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target, info


def stream_backup(database: str, out: BinaryIO) -> DatabaseInfo:
    """Write a backup to a binary stream (stdout), so it can land outside a container."""
    with tempfile.TemporaryDirectory(prefix="river-gateway-backup-") as tmp:
        path, info = create_backup(database, Path(tmp) / "gateway.db")
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, out)
        out.flush()
    return info


def list_backups(database: str) -> list[Path]:
    """Backups in backup_dir, newest first (including pre-restore copies)."""
    directory = backup_dir(database)
    if not directory.is_dir():
        return []
    files = [p for p in directory.glob("*.db") if p.name.startswith((BACKUP_PREFIX, SAFETY_PREFIX))]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def prune_backups(database: str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` regular backups; pre-restore copies are never pruned."""
    regular = [p for p in list_backups(database) if p.name.startswith(BACKUP_PREFIX)]
    removed = regular[keep:]
    for path in removed:
        path.unlink()
    return removed


def restore_backup(database: str, backup: Path) -> tuple[Path | None, DatabaseInfo]:
    """Replace the database with `backup`. Returns the pre-restore copy (if any) and the result."""
    info = inspect(backup)
    if not info.ok:
        raise BackupError(f"{backup} is not a usable SQLite database: {info.error}")
    missing = REQUIRED_TABLES - info.tables
    if missing:
        raise BackupError(f"{backup} is not a River Gateway database (missing tables: {', '.join(sorted(missing))})")
    target = Path(database)
    safety = None
    if target.is_file():
        safety, _ = create_backup(database, _unique_path(backup_dir(database), SAFETY_PREFIX))
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(backup)
    dst = sqlite3.connect(target, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    # Bring a backup from an older version up to the current schema, and back to WAL mode.
    Store(str(target)).close()
    return safety, inspect(target)


def restore_from_stream(database: str, stream: BinaryIO) -> tuple[Path | None, DatabaseInfo]:
    """Restore from a binary stream (stdin), e.g. a backup piped into `docker compose exec -T`."""
    with tempfile.TemporaryDirectory(prefix="river-gateway-restore-") as tmp:
        upload = Path(tmp) / "upload.db"
        with upload.open("wb") as handle:
            shutil.copyfileobj(stream, handle)
        if upload.stat().st_size == 0:
            raise BackupError("no backup data arrived on stdin")
        return restore_backup(database, upload)
