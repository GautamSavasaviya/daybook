"""Time off (SPEC A6, D-048): leave and holidays, full or half days."""

from datetime import date, timedelta
from gettext import gettext as _
from gettext import ngettext

from daybook.core import validation
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import ChangedSinceError, NotFoundError, ValidationError
from daybook.core.models import TaskStatus, TaskSummary, TimeOff, TimeOffPart
from daybook.core.repo import leave as leave_repo
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_off as time_off_repo
from daybook.core.tasklists import ListKind, TaskQuery
from daybook.core.timeutil import fmt_day
from daybook.services.calendar import CalendarService
from daybook.services.events import Changed, Entity, EventBus, Toast

MAX_LEAVE_DAYS = 60


class TimeOffService:
    def __init__(
        self, conn: Connection, bus: EventBus, clock: Clock, calendar: CalendarService
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._calendar = calendar

    def today(self) -> date:
        return self._clock.now().astimezone(self._clock.local_tz()).date()

    def upcoming(self) -> list[TimeOff]:
        return time_off_repo.from_day(self._conn, self.today())

    def _days(self, first: date, last: date, part: TimeOffPart) -> list[date]:
        if first < self.today():
            raise ValidationError("first_day", _("Pick today or a later date."))
        if last < first:
            raise ValidationError("last_day", _("The last day can't be before the first."))
        count = (last - first).days + 1
        if count > MAX_LEAVE_DAYS:
            raise ValidationError(
                "last_day", _("Mark at most {n} days at a time.").format(n=MAX_LEAVE_DAYS)
            )
        if part != TimeOffPart.FULL and count > 1:
            raise ValidationError("part", _("A half day off is a single day."))
        return [first + timedelta(days=offset) for offset in range(count)]

    def due_during(self, first: date, last: date) -> list[TaskSummary]:
        """Open tasks that fall due while away, so they can be moved too (SPEC A6)."""
        inbox = tasks_repo.list_tasks(self._conn, TaskQuery(ListKind.UPCOMING, self.today()))
        return [
            s
            for s in inbox
            if s.task.due_date is not None
            and first <= s.task.due_date <= last
            and s.task.status != TaskStatus.ON_HOLD
        ]

    def mark(self, first: date, last: date, part: TimeOffPart, note: str = "") -> int:
        """Mark days off. Plans on full days off move to the next working day (with Undo).
        Weekends and other non-workdays inside a range are skipped. Returns tasks moved."""
        clean_note = validation.optional_text(note, "note") or ""
        calendar = self._calendar.calendar()
        days = [d for d in self._days(first, last, part) if calendar.is_workday(d)]
        if not days:
            raise ValidationError("first_day", _("Those days are not workdays anyway."))
        entries = [TimeOff(day, part, clean_note) for day in days]
        previous = {day: time_off_repo.get(self._conn, day) for day in days}
        after_calendar = calendar.with_time_off(dict.fromkeys(days, part))
        today = self.today()
        before = leave_repo.mark(self._conn, entries, after_calendar.next_working_day, today)
        after = [tasks_repo.capture(self._conn, s.task.id, s.plan_from) for s in before]
        self._changed()

        def undo() -> None:
            try:
                leave_repo.undo(self._conn, previous, list(zip(before, after, strict=True)))
            except NotFoundError, ChangedSinceError:
                self._bus.publish(Toast(_("Couldn't undo: the plan has changed since")))
                return
            self._changed()

        when = fmt_day(days[0], today)
        if len(days) > 1:
            when = _("{first} to {last}").format(first=when, last=fmt_day(days[-1], today))
        message = _("Marked {when} off").format(when=when)
        if before:
            message += " · " + ngettext("{n} task moved", "{n} tasks moved", len(before)).format(
                n=len(before)
            )
        self._bus.publish(Toast(message, undo=undo))
        return len(before)

    def remove(self, day: date) -> None:
        previous = time_off_repo.get(self._conn, day)
        if previous is None:
            return
        time_off_repo.remove(self._conn, day)
        self._changed()

        def undo() -> None:
            time_off_repo.put(self._conn, previous)
            self._changed()

        self._bus.publish(
            Toast(
                _("{day} is a working day again").format(day=fmt_day(day, self.today())),
                undo=undo,
            )
        )

    def _changed(self) -> None:
        # The calendar cache is dropped on TIME_OFF; plans may have moved too.
        self._bus.publish(Changed(Entity.TIME_OFF))
        self._bus.publish(Changed(Entity.PLAN))
        self._bus.publish(Changed(Entity.TASK))
