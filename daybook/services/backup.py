"""Backups, restore, and JSON export/import (D-026, D-036). One place, because restoring and
importing end the same way: the data is replaced in place, and everything that cached
something re-reads it."""

import logging
from collections.abc import Callable
from concurrent.futures import Future
from datetime import date
from pathlib import Path

from daybook.core import backup, exchange, timeutil
from daybook.core.backup import BackupFile, Kind, Verified
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.repo import app_state, reporting
from daybook.core.repo import reminders as reminder_log
from daybook.services.events import Changed, DayChanged, Entity, EventBus
from daybook.services.rollover import RolloverService
from daybook.services.safe import report_error
from daybook.services.scheduler import ReminderScheduler
from daybook.services.settings import SettingsService
from daybook.services.timer import TimerService
from daybook.services.worker import Worker

log = logging.getLogger(__name__)

LAST_BACKUP = "last_backup_date"

type Done[T] = Callable[[T], None]
type Failed = Callable[[BaseException], None]


class BackupService:
    def __init__(
        self,
        conn: Connection,
        bus: EventBus,
        clock: Clock,
        *,
        directory: Path,
        worker: Worker,
        settings: SettingsService,
        timer: TimerService,
        rollover: RolloverService,
        reminders: ReminderScheduler,
    ) -> None:
        self._reminders = reminders
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._directory = directory
        self._worker = worker
        self._settings = settings
        self._timer = timer
        self._rollover = rollover
        self._running = False
        bus.subscribe(DayChanged, self._on_day)

    # Listing and the daily backup -------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._directory

    def backups(self) -> list[BackupFile]:
        return backup.list_backups(self._directory)

    def last_backup_date(self) -> date | None:
        value = app_state.get(self._conn, LAST_BACKUP)
        try:
            return date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            log.warning("ignoring a damaged %s: %r", LAST_BACKUP, value)
            return None

    def _today(self) -> date:
        return self._clock.now().astimezone(self._clock.local_tz()).date()

    def run_if_due(self) -> Future[BackupFile] | None:
        """The automatic backup, once per local day, taken on the worker so the window
        never waits for it. Returns its future, or None if none was needed."""
        today = self._today()
        if self._running or not backup.is_due(self.last_backup_date(), today):
            return None
        self._running = True
        now = self._clock.now()
        directory = self._directory

        def job(conn: Connection) -> BackupFile:
            return backup.create(conn, directory, now, Kind.AUTOMATIC)

        def done(_made: BackupFile) -> None:
            self._running = False
            app_state.put(self._conn, LAST_BACKUP, today.isoformat())
            self._bus.publish(Changed(Entity.SETTINGS))

        def failed(error: BaseException) -> None:
            self._running = False  # tried again at the next day change or start
            report_error(error)

        return self._worker.submit(job, done, failed)

    def _on_day(self, _event: DayChanged) -> None:
        self.run_if_due()

    def backup_now(self) -> BackupFile:
        made = backup.create(self._conn, self._directory, self._clock.now(), Kind.MANUAL)
        app_state.put(self._conn, LAST_BACKUP, self._today().isoformat())
        self._bus.publish(Changed(Entity.SETTINGS))
        return made

    # Replacing the data -----------------------------------------------------------------

    # Everything below can take a second at 5,000 tasks and 50,000 time entries, so it runs
    # on the worker thread (CLAUDE.md: nothing blocks the main loop for 100 ms). Each takes
    # `on_done`/`on_error`, called on the main thread, and returns the future.

    def check(self, path: Path, on_done: Done[Verified], on_error: Failed) -> Future[Verified]:
        """Look inside a backup before offering to restore it (BackupError on a bad file)."""
        return self._worker.submit(lambda _conn: backup.verify(path), on_done, on_error)

    def restore(self, path: Path, on_done: Done[Verified], on_error: Failed) -> Future[Verified]:
        """Replace everything with a backup, after saving a safety copy of what is there
        (D-036). Nothing changes if the file is refused."""
        directory = self._directory
        now = self._clock.now()

        def job(conn: Connection) -> Verified:
            checked = backup.verify(path)
            backup.create(conn, directory, now, Kind.SAFETY)
            backup.restore(conn, path, now)
            return checked

        def done(checked: Verified) -> None:
            self._data_replaced()
            on_done(checked)

        return self._worker.submit(job, done, on_error)

    def export_json(self, on_done: Done[str], on_error: Failed) -> Future[str]:
        now = self._clock.now()
        return self._worker.submit(lambda conn: exchange.export_data(conn, now), on_done, on_error)

    def check_import(
        self, text: str, on_done: Done[dict[str, int]], on_error: Failed
    ) -> Future[dict[str, int]]:
        """Read an export file without changing anything: how much is in it, or
        `ImportRefusedError` saying where and why it can't be imported."""
        now = self._clock.now()

        def job(_conn: Connection) -> dict[str, int]:
            checked = exchange.validate(text, now)
            checked.close()
            return checked.counts

        return self._worker.submit(job, on_done, on_error)

    def import_json(
        self, text: str, on_done: Done[dict[str, int]], on_error: Failed
    ) -> Future[dict[str, int]]:
        """Replace everything with an export file (D-026). The whole file is checked first;
        if anything is wrong `ImportRefusedError` says where, and nothing is touched."""
        directory = self._directory
        now = self._clock.now()

        def job(conn: Connection) -> dict[str, int]:
            checked = exchange.validate(text, now)
            try:
                backup.create(conn, directory, now, Kind.SAFETY)
                backup.replace_contents(conn, checked.conn, now)
            finally:
                checked.close()
            return checked.counts

        def done(counts: dict[str, int]) -> None:
            self._data_replaced()
            on_done(counts)

        return self._worker.submit(job, done, on_error)

    def time_entries_csv(self, on_done: Done[str], on_error: Failed) -> Future[str]:
        tz = self._clock.local_tz()

        def job(conn: Connection) -> str:
            return exchange.time_entries_csv(reporting.all_time_entries(conn), tz)

        return self._worker.submit(job, on_done, on_error)

    def _data_replaced(self) -> None:
        """Everything that cached or watched the old data starts over from the new."""
        now = self._clock.now()
        self._settings.reload()
        self._timer.data_replaced()
        self._reminders.reset()  # cards and banners about the old data must not linger
        # Nothing "was missed" in data that was just put in place: no summary, no flood.
        reminder_log.set_watermark(self._conn, now)
        self._rollover.run()
        for entity in Entity:
            self._bus.publish(Changed(entity))
        log.info("data replaced at %s", timeutil.to_db(now))
