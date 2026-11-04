"""Backups, restore, and JSON export/import (D-026, D-036). One place, because restoring and
importing end the same way: the data is replaced in place, and everything that cached
something re-reads it."""

import logging
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
from daybook.services.settings import SettingsService
from daybook.services.timer import TimerService
from daybook.services.worker import Worker

log = logging.getLogger(__name__)

LAST_BACKUP = "last_backup_date"


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
    ) -> None:
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
        return date.fromisoformat(value) if isinstance(value, str) else None

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

    def check(self, path: Path) -> Verified:
        """Look inside a backup before offering to restore it. Raises BackupError."""
        return backup.verify(path)

    def restore(self, path: Path) -> Verified:
        """Replace everything with a backup, after saving a safety copy of what is there
        (D-036). Nothing changes if the file is refused."""
        checked = backup.verify(path)
        backup.create(self._conn, self._directory, self._clock.now(), Kind.SAFETY)
        backup.restore(self._conn, path, self._clock.now())
        self._data_replaced()
        return checked

    def export_json(self) -> str:
        return exchange.export_data(self._conn, self._clock.now())

    def check_import(self, text: str) -> dict[str, int]:
        """Read an export file without changing anything: how much is in it, or
        `ImportRefusedError` saying where and why it can't be imported."""
        checked = exchange.validate(text, self._clock.now())
        checked.close()
        return checked.counts

    def import_json(self, text: str) -> dict[str, int]:
        """Replace everything with an export file (D-026). The whole file is checked first;
        if anything is wrong `ImportRefusedError` says where, and nothing is touched."""
        checked = exchange.validate(text, self._clock.now())
        try:
            backup.create(self._conn, self._directory, self._clock.now(), Kind.SAFETY)
            backup.replace_contents(self._conn, checked.conn, self._clock.now())
        finally:
            checked.close()
        self._data_replaced()
        return checked.counts

    def time_entries_csv(self) -> str:
        return exchange.time_entries_csv(
            reporting.all_time_entries(self._conn), self._clock.local_tz()
        )

    def _data_replaced(self) -> None:
        """Everything that cached or watched the old data starts over from the new."""
        now = self._clock.now()
        self._settings.reload()
        self._timer.data_replaced()
        # Nothing "was missed" in data that was just put in place: no summary, no flood.
        reminder_log.set_watermark(self._conn, now)
        self._rollover.run()
        for entity in Entity:
            self._bus.publish(Changed(entity))
        log.info("data replaced at %s", timeutil.to_db(now))
