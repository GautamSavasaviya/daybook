"""Runs the automatic carry-over once per working day (SPEC A5, D-045, PLAN §5.2).

At startup and whenever the day changes: if today is a working day and the rollover
hasn't run for it yet, unfinished items planned since the last rollover are added to
today's plan with their remaining time.
"""

import logging
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from daybook.core import carryover, timeutil
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.models import TaskStatus
from daybook.core.repo import app_state
from daybook.core.repo import plan as plan_repo
from daybook.core.repo import time_entries as time_repo
from daybook.services.calendar import CalendarService
from daybook.services.events import Changed, DayChanged, Entity, EventBus
from daybook.services.settings import SettingsService

log = logging.getLogger(__name__)

WATERMARK = "last_rollover_date"
# On the very first run there is no watermark; look back this far (D-081).
FIRST_RUN_LOOKBACK = timedelta(days=31)


class RolloverService:
    def __init__(
        self,
        conn: Connection,
        bus: EventBus,
        clock: Clock,
        settings: SettingsService,
        calendar: CalendarService,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._settings = settings
        self._calendar = calendar
        bus.subscribe(DayChanged, self._on_day_changed)

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.run()

    def _watermark(self) -> date | None:
        value = app_state.get(self._conn, WATERMARK)
        if not isinstance(value, str):
            return None
        try:
            return timeutil.date_from_db(value)
        except ValueError:
            log.warning("ignoring a damaged %s: %r", WATERMARK, value)
            return None

    def run(self) -> int:
        """Carry over if it is due. Returns how many items were added to today."""
        tz = self._clock.local_tz()
        today = self._clock.now().astimezone(tz).date()
        if not self._calendar.calendar().is_working(today):
            return 0  # runs on the next working day instead
        last = self._watermark()
        if last is not None and last >= today:
            return 0
        if not self._settings.current.auto_carry_over:
            # Turned off: nothing carries, and turning it back on later won't bring back
            # old days (D-081).
            plan_repo.apply_rollover(self._conn, today, [], WATERMARK)
            return 0
        after = last if last is not None else today - FIRST_RUN_LOOKBACK
        rows = plan_repo.since(self._conn, after, today)
        tracked = self._tracked(rows)
        sources = [
            carryover.SourceItem(
                plan_date=timeutil.date_from_db(row["plan_date"]),
                position=row["position"],
                task_id=row["task_id"],
                allotted_min=row["allotted_min"],
                carry_count=row["carry_count"],
                tracked_min=tracked.get((row["plan_date"], row["task_id"]), 0),
                status=TaskStatus(row["status"]),
                deleted=bool(row["deleted"]),
            )
            for row in rows
        ]
        planned_today = {item.task_id for item in plan_repo.items_on(self._conn, today)}
        carried = carryover.rollover(sources, planned_today, today)
        plan_repo.apply_rollover(self._conn, today, carried, WATERMARK)
        if carried:
            log.info("carried over %d item(s) to %s", len(carried), today)
            self._bus.publish(Changed(Entity.PLAN))
            self._bus.publish(Changed(Entity.TASK))
        return len(carried)

    def _tracked(self, rows: list[sqlite3.Row]) -> dict[tuple[str, int], int]:
        """Minutes tracked per (stored plan date, task) on that day."""
        by_day: defaultdict[str, list[int]] = defaultdict(list)
        for row in rows:
            by_day[row["plan_date"]].append(row["task_id"])
        result: dict[tuple[str, int], int] = {}
        now = self._clock.now()
        for stored_day, task_ids in by_day.items():
            minutes = time_repo.tracked_on_day(
                self._conn, task_ids, timeutil.date_from_db(stored_day), self._clock.local_tz(), now
            )
            for task_id, value in minutes.items():
                result[(stored_day, task_id)] = value
        return result
