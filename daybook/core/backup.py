"""Database backups (D-036): files in `backups/`, rotation, verification and restore.

Backups use SQLite's online backup API, so they are consistent while Daybook runs. Names are
``daybook-YYYYMMDD-HHMMSS.db`` in UTC (``…-safety.db`` for the copy taken before a restore
or import). The 14 newest automatic and manual backups are kept; safety copies are kept
separately, the newest 3.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from daybook.core.db import Connection, connect
from daybook.core.errors import DaybookError, SchemaTooNewError
from daybook.core.migrations import LATEST_VERSION, migrate, schema_version

log = logging.getLogger(__name__)

KEEP = 14
KEEP_SAFETY = 3
_NAME = re.compile(r"daybook-(\d{8})-(\d{6})(-safety)?\.db")
_STAMP = "%Y%m%d-%H%M%S"
# A file without these isn't a Daybook database, whatever its integrity.
_REQUIRED_TABLES = frozenset({"project", "task", "time_entry", "settings"})


class BackupError(DaybookError):
    """A backup or restore failed or was refused; the message says why."""


class Kind(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    SAFETY = "safety"


@dataclass(frozen=True)
class BackupFile:
    path: Path
    made_at: datetime
    safety: bool
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def file_name(now: datetime, *, safety: bool = False) -> str:
    stamp = now.astimezone(UTC).strftime(_STAMP)
    return f"daybook-{stamp}{'-safety' if safety else ''}.db"


def _parse(path: Path) -> BackupFile | None:
    match = _NAME.fullmatch(path.name)
    if match is None:
        return None
    made = datetime.strptime(f"{match[1]}-{match[2]}", _STAMP).replace(tzinfo=UTC)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return BackupFile(path, made, bool(match[3]), size)


def list_backups(directory: Path) -> list[BackupFile]:
    """Backups in `directory`, newest first. Other files there are left alone."""
    if not directory.is_dir():
        return []
    found = [b for p in directory.iterdir() if (b := _parse(p)) is not None]
    return sorted(found, key=lambda b: (b.made_at, b.name), reverse=True)


def is_due(last_backup: date | None, today: date) -> bool:
    """One automatic backup per local day, at the first chance."""
    return last_backup is None or last_backup < today


@dataclass(frozen=True)
class Verified:
    schema_version: int
    tasks: int
    projects: int


def verify(path: Path) -> Verified:
    """Check a file is an intact Daybook database this version can use. Raises BackupError.

    The file is opened read-only, so a refused file is never modified."""
    try:
        conn = connect(path, read_only=True)
    except sqlite3.Error as exc:
        raise BackupError(f"Can't open {path.name}: {exc}") from exc
    try:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            version = schema_version(conn)
        except (sqlite3.DatabaseError, UnicodeDecodeError) as exc:
            raise BackupError(f"{path.name} is not a readable database ({exc}).") from exc
        if result != "ok":
            raise BackupError(f"{path.name} is damaged ({result}).")
        if not tables >= _REQUIRED_TABLES:
            raise BackupError(f"{path.name} is not a Daybook database.")
        if version > LATEST_VERSION:
            raise SchemaTooNewError(version, LATEST_VERSION)
        tasks = conn.execute("SELECT count(*) FROM task").fetchone()[0]
        projects = conn.execute("SELECT count(*) FROM project").fetchone()[0]
        return Verified(version, int(tasks), int(projects))
    finally:
        conn.close()


def create(source: sqlite3.Connection, directory: Path, now: datetime, kind: Kind) -> BackupFile:
    """Copy the database into `directory`, check the copy, and rotate old ones.

    Written under a temporary name first, so a crash never leaves a half file that looks
    like a backup."""
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / file_name(now, safety=kind == Kind.SAFETY)
    if final.exists():  # two in the same second: the first one is as good
        found = _parse(final)
        if found is not None:
            return found
    temp = directory / f".{final.name}.partial"
    temp.unlink(missing_ok=True)
    target = sqlite3.connect(temp)
    try:
        source.backup(target)
    except sqlite3.Error as exc:
        target.close()
        temp.unlink(missing_ok=True)
        raise BackupError(f"The backup failed: {exc}") from exc
    # A backup is a single file: fold the WAL in and leave no -wal/-shm beside it.
    target.execute("PRAGMA journal_mode = DELETE")
    target.close()
    try:
        verify(temp)
    except DaybookError:
        temp.unlink(missing_ok=True)
        raise
    temp.replace(final)
    rotate(directory)
    found = _parse(final)
    assert found is not None
    log.info("backup written: %s", final.name)
    return found


def rotate(directory: Path, keep: int = KEEP, keep_safety: int = KEEP_SAFETY) -> list[Path]:
    """Delete the oldest backups beyond the limits. Returns what was removed."""
    backups = list_backups(directory)
    regular = [b for b in backups if not b.safety]
    safety = [b for b in backups if b.safety]
    removed: list[Path] = []
    for old in (*regular[keep:], *safety[keep_safety:]):
        try:
            old.path.unlink()
        except OSError:
            log.warning("could not remove old backup %s", old.path, exc_info=True)
        else:
            removed.append(old.path)
    return removed


def replace_contents(live: Connection, source: sqlite3.Connection, now: datetime) -> None:
    """Make the open database `live` hold exactly what `source` holds, in place.

    Services keep their connection; the data underneath changes (D-119). The result is
    migrated to the latest schema and re-checked."""
    if live.tx_depth:
        raise BackupError("The database is busy; try again in a moment.")
    try:
        source.backup(live)
    except sqlite3.Error as exc:
        raise BackupError(f"The data could not be replaced: {exc}") from exc
    live.execute("PRAGMA foreign_keys = ON")
    migrate(live, now)
    problems = live.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise BackupError("The restored data has broken references.")


def restore(live: Connection, path: Path, now: datetime) -> Verified:
    """Replace the live data with a backup file. The caller takes the safety copy first."""
    checked = verify(path)
    source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        replace_contents(live, source, now)
    finally:
        source.close()
    log.info("restored %s (%d tasks)", path.name, checked.tasks)
    return checked
