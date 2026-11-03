"""AppContext: opens the database and wires the services together."""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Self

from daybook.core.clock import Clock
from daybook.core.db import Connection, connect
from daybook.core.errors import SchemaTooNewError
from daybook.core.migrations import LATEST_VERSION, migrate, schema_version
from daybook.paths import Paths
from daybook.services.calendar import CalendarService
from daybook.services.day import DayWatcher
from daybook.services.events import CallSoon, EventBus
from daybook.services.projects import ProjectService
from daybook.services.settings import SettingsService
from daybook.services.tasks import TaskService
from daybook.services.worker import Worker

log = logging.getLogger(__name__)


def _check_not_newer(paths: Paths) -> None:
    """Refuse a database from a newer Daybook before opening it read-write.

    A read-write open would switch it to WAL and could change the file; this check uses a
    read-only connection so a refused database stays byte-for-byte untouched.
    """
    if not paths.db_file.exists():
        return
    probe = connect(paths.db_file, read_only=True)
    try:
        found = schema_version(probe)
    finally:
        probe.close()
    if found > LATEST_VERSION:
        raise SchemaTooNewError(found, LATEST_VERSION)


@dataclass
class AppContext:
    paths: Paths
    clock: Clock
    conn: Connection
    bus: EventBus
    settings: SettingsService
    worker: Worker
    calendar: CalendarService
    projects: ProjectService
    tasks: TaskService
    day: DayWatcher

    @classmethod
    def open(cls, paths: Paths, clock: Clock, call_soon: CallSoon) -> Self:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        _check_not_newer(paths)
        conn = connect(paths.db_file)
        try:
            applied = migrate(conn, clock.now())
            if applied:
                log.info("database migrated (%d step(s)) at %s", applied, paths.db_file)
            bus = EventBus(call_soon)
            settings = SettingsService(conn, bus)
            worker = Worker(paths.db_file, call_soon)
            calendar = CalendarService(conn, settings, bus)
            context = cls(
                paths=paths,
                clock=clock,
                conn=conn,
                bus=bus,
                settings=settings,
                worker=worker,
                calendar=calendar,
                projects=ProjectService(conn, bus, clock),
                tasks=TaskService(conn, bus, clock, worker, calendar),
                day=DayWatcher(clock, bus),
            )
            context.tasks.purge_deleted()
            return context
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        """Stop background work and close the database cleanly."""
        self.day.stop()
        self.worker.shutdown()
        try:
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            log.exception("PRAGMA optimize failed during shutdown")
        self.conn.close()
        log.info("database closed")
