"""Review, standup, timesheet and weekly reports: reads shaped by `core/` modules.

The day-sized ones are quick and run in place; the weekly report reads a week of entries,
plans and meetings, so it runs on the worker (PLAN §2).
"""

from collections.abc import Callable
from concurrent.futures import Future
from datetime import date, tzinfo

from daybook.core import hold as hold_rules
from daybook.core import reports, standup, timesheet, timeutil
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.models import HoldReason
from daybook.core.repo import reporting
from daybook.core.reports import WeeklyReport
from daybook.core.standup import Standup
from daybook.core.timesheet import Timesheet
from daybook.services.calendar import CalendarService
from daybook.services.plan import PlanService
from daybook.services.settings import SettingsService
from daybook.services.worker import Worker


def _weekly(conn: Connection, days: tuple[date, ...], tz: tzinfo, now_clock: Clock) -> WeeklyReport:
    now = now_clock.now()
    start, _first_end = timeutil.local_day_bounds(days[0], tz)
    _last_start, end = timeutil.local_day_bounds(days[-1], tz)
    slices = reporting.slices(conn, start, end, now)
    plan = reporting.planned_between(conn, days[0], days[-1])
    completed = len(reporting.completed_between(conn, start, end))
    occurrences = reporting.occurrences_between(conn, start, end, tz)
    meetings = reports.meeting_minutes(occurrences, start, end, now)
    return reports.build(days, tz, slices, plan, completed=completed, meetings=meetings)


class ReportService:
    def __init__(
        self,
        conn: Connection,
        clock: Clock,
        *,
        settings: SettingsService,
        calendar: CalendarService,
        plan: PlanService,
        worker: Worker,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._settings = settings
        self._calendar = calendar
        self._plan = plan
        self._worker = worker

    def today(self) -> date:
        return self._plan.today()

    # Timesheet --------------------------------------------------------------------------

    def timesheet(self, days: tuple[date, ...]) -> Timesheet:
        tz = self._clock.local_tz()
        start, _unused = timeutil.local_day_bounds(days[0], tz)
        _unused, end = timeutil.local_day_bounds(days[-1], tz)
        slices = reporting.slices(self._conn, start, end, self._clock.now())
        return timesheet.build(slices, days, tz, self._settings.current.timesheet_rounding_min)

    # Standup ----------------------------------------------------------------------------

    def standup(self) -> Standup:
        tz = self._clock.local_tz()
        today = self.today()
        now = self._clock.now()
        last_day = self._calendar.calendar().prev_working_day(today)
        start, end = timeutil.local_day_bounds(last_day, tz)
        slices = reporting.slices(self._conn, start, end, now)
        done = {d.task_id: d for d in reporting.completed_between(self._conn, start, end)}
        seconds: dict[int, int] = {}
        names: dict[int, str] = {}
        for item in slices:
            overlap = min(item.end, end) - max(item.start, start)
            seconds[item.task_id] = seconds.get(item.task_id, 0) + int(overlap.total_seconds())
            names[item.task_id] = (
                f"{item.ticket_key} {item.title}" if item.ticket_key else item.title
            )
        requested = {
            e.summary.task.id: e.summary.task.requested_by for e in self._plan.entries(last_day)
        }
        worked = [
            standup.Worked(
                done[tid].name if tid in done else names[tid],
                seconds.get(tid, 0),
                tid in done,
                done[tid].requested_by if tid in done else requested.get(tid),
            )
            for tid in sorted(set(seconds) | set(done))
        ]
        planned = [
            standup.Planned(
                f"{e.summary.task.ticket_key} {e.summary.task.title}"
                if e.summary.task.ticket_key
                else e.summary.task.title,
                e.item.allotted_min,
                e.summary.task.requested_by,
            )
            for e in self._plan.entries(today)
            if e.open
        ]
        blockers = [
            standup.Blocker(
                held.name,
                hold_rules.reason_label(HoldReason(held.reason)),
                max(0, (today - held.since.astimezone(tz).date()).days),
                held.note,
            )
            for held in reporting.on_hold(self._conn)
        ]
        return standup.build(worked, planned, blockers, last_day=last_day, today=today)

    # Weekly report ----------------------------------------------------------------------

    def week_of(self, day: date) -> tuple[date, ...]:
        return timesheet.week_days(day)

    def weekly(
        self, days: tuple[date, ...], on_done: Callable[[WeeklyReport], None]
    ) -> Future[WeeklyReport]:
        tz = self._clock.local_tz()
        clock = self._clock
        return self._worker.submit(lambda conn: _weekly(conn, days, tz, clock), on_done)
