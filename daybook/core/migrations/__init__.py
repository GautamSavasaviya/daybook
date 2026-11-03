"""Versioned schema migrations.

``PRAGMA user_version`` holds the schema version. Each migration runs in its own
transaction together with the version bump, so a failure leaves the database exactly at
the previous version. Released migrations are never edited; changes go in a new module
``m000N_<name>.py`` registered in `MIGRATIONS` (CLAUDE.md).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from daybook.core import timeutil
from daybook.core.db import Connection, transaction
from daybook.core.errors import MigrationError, SchemaTooNewError
from daybook.core.migrations import m0001_initial

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationContext:
    now: str  # stored-format UTC timestamp, for seed rows


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection, MigrationContext], None]


MIGRATIONS: tuple[Migration, ...] = (Migration(1, "initial schema", m0001_initial.apply),)
LATEST_VERSION = MIGRATIONS[-1].version


def schema_version(conn: Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: Connection, now: datetime, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Bring the database up to the latest version. Returns how many steps ran.

    Raises `SchemaTooNewError` (without touching the file) if the database comes from a
    newer Daybook, and `MigrationError` if a step fails (that step is rolled back).
    """
    latest = migrations[-1].version if migrations else 0
    current = schema_version(conn)
    if current > latest:
        raise SchemaTooNewError(current, latest)
    context = MigrationContext(now=timeutil.to_db(now))
    applied = 0
    for migration in migrations:
        if migration.version <= current:
            continue
        log.info("migrating database to v%d (%s)", migration.version, migration.name)
        try:
            with transaction(conn):
                migration.apply(conn, context)
                conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        except Exception as exc:
            raise MigrationError(
                f"migration to v{migration.version} ({migration.name}) failed: {exc}"
            ) from exc
        applied += 1
    return applied
