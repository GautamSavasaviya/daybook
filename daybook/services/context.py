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
from daybook.services.backup import BackupService
from daybook.services.calendar import CalendarService
from daybook.services.day import DayWatcher
from daybook.services.events import CallSoon, EventBus
from daybook.services.idle import IdleService
from daybook.services.meetings import MeetingService
from daybook.services.notifier import Notifier
from daybook.services.onboarding import OnboardingService
from daybook.services.plan import PlanService
from daybook.services.power import PowerMonitor
from daybook.services.projects import ProjectService
from daybook.services.reports import ReportService
from daybook.services.rollover import RolloverService
from daybook.services.scheduler import ReminderScheduler
from daybook.services.settings import SettingsService
from daybook.services.tasks import TaskService
from daybook.services.time_off import TimeOffService
from daybook.services.timer import TimerService
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
    meetings: MeetingService
    plan: PlanService
    time_off: TimeOffService
    timer: TimerService
    rollover: RolloverService
    day: DayWatcher
    notifier: Notifier
    reminders: ReminderScheduler
    reports: ReportService
    backup: BackupService
    onboarding: OnboardingService
    idle: IdleService
    power: PowerMonitor

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
            timer = TimerService(conn, bus, clock)
            tasks = TaskService(
                conn, bus, clock, worker, calendar, settle_timer=timer.settle_recovery
            )
            meetings = MeetingService(conn, bus, clock, settings)
            plan = PlanService(
                conn,
                bus,
                clock,
                worker=worker,
                calendar=calendar,
                meetings=meetings,
                tasks=tasks,
            )
            notifier = Notifier(bus, settings)
            scheduler = ReminderScheduler(
                conn,
                bus,
                clock,
                settings=settings,
                calendar=calendar,
                tasks=tasks,
                plan=plan,
                meetings=meetings,
                timer=timer,
                notifier=notifier,
            )
            day = DayWatcher(clock, bus)
            rollover = RolloverService(conn, bus, clock, settings, calendar)
            projects = ProjectService(conn, bus, clock)

            def resumed() -> None:
                try:
                    day.check()
                finally:  # the reminder check must still happen if the date check fails
                    scheduler.resumed()

            context = cls(
                paths=paths,
                clock=clock,
                conn=conn,
                bus=bus,
                settings=settings,
                worker=worker,
                calendar=calendar,
                projects=projects,
                tasks=tasks,
                meetings=meetings,
                plan=plan,
                time_off=TimeOffService(conn, bus, clock, calendar),
                timer=timer,
                rollover=rollover,
                day=day,
                notifier=notifier,
                reminders=scheduler,
                reports=ReportService(
                    conn, clock, settings=settings, calendar=calendar, plan=plan, worker=worker
                ),
                backup=BackupService(
                    conn,
                    bus,
                    clock,
                    directory=paths.backups_dir,
                    worker=worker,
                    settings=settings,
                    timer=timer,
                    rollover=rollover,
                    reminders=scheduler,
                ),
                onboarding=OnboardingService(
                    conn, clock, settings=settings, projects=projects, tasks=tasks
                ),
                idle=IdleService(bus, clock, settings=settings, timer=timer),
                power=PowerMonitor(on_sleep=timer.beat, on_resume=resumed, on_shutdown=timer.beat),
            )
            context.tasks.purge_deleted()
            context.meetings.purge_deleted()
            context.rollover.run()
            return context
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        """Stop background work, save the timer and close the database cleanly."""
        self.day.stop()
        self.reminders.stop()
        self.idle.stop()
        self.power.stop()
        try:
            self.timer.shutdown()
        except Exception:
            log.exception("could not save the timer during shutdown")
        self.worker.shutdown()
        try:
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            log.exception("PRAGMA optimize failed during shutdown")
        self.conn.close()
        log.info("database closed")
