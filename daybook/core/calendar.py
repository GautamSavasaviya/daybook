"""WorkCalendar: the only code that decides which days are working days and when the
working day starts and ends (CLAUDE.md).

It knows the workdays, working hours, lunch break and time off (D-048). A full day off is
not a working day. A half day off still is, with a shorter window: morning off starts the
day after lunch, afternoon off ends it at lunch (at the midpoint without a lunch break).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from types import MappingProxyType

from daybook.core.models import TimeOffPart
from daybook.core.settings import Settings
from daybook.core.timeutil import localize

_NO_TIME_OFF: Mapping[date, TimeOffPart] = MappingProxyType({})


@dataclass(frozen=True)
class Interval:
    """A span of time; both ends are aware datetimes in UTC, `start < end`."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


def _at(day: date, clock: time, tz: tzinfo) -> datetime:
    # DST gaps move forward, repeated hours take the first one (timeutil.localize).
    return localize(datetime.combine(day, clock), tz).astimezone(UTC)


class WorkCalendar:
    def __init__(
        self,
        workdays: frozenset[int],
        time_off: Mapping[date, TimeOffPart] = _NO_TIME_OFF,
        *,
        work_start: time = time(9, 0),
        work_end: time = time(18, 0),
        lunch_start: time = time(13, 0),
        lunch_minutes: int = 60,
    ) -> None:
        if not workdays:
            raise ValueError("at least one workday is required")
        self._workdays = workdays
        self._time_off = MappingProxyType(dict(time_off))
        self._work_start = work_start
        self._work_end = work_end
        self._lunch_start = lunch_start
        self._lunch_minutes = lunch_minutes

    @classmethod
    def from_settings(
        cls, settings: Settings, time_off: Mapping[date, TimeOffPart] = _NO_TIME_OFF
    ) -> WorkCalendar:
        return cls(
            settings.workdays,
            time_off,
            work_start=settings.work_start,
            work_end=settings.work_end,
            lunch_start=settings.lunch_start,
            lunch_minutes=settings.lunch_minutes,
        )

    def with_time_off(self, changes: Mapping[date, TimeOffPart | None]) -> WorkCalendar:
        """A copy with some days' time off set (or removed, for None)."""
        merged = dict(self._time_off)
        for day, part in changes.items():
            if part is None:
                merged.pop(day, None)
            else:
                merged[day] = part
        return WorkCalendar(
            self._workdays,
            merged,
            work_start=self._work_start,
            work_end=self._work_end,
            lunch_start=self._lunch_start,
            lunch_minutes=self._lunch_minutes,
        )

    # Days -------------------------------------------------------------------------------

    def time_off(self, day: date) -> TimeOffPart | None:
        return self._time_off.get(day)

    def is_workday(self, day: date) -> bool:
        """A workday by the settings, ignoring time off."""
        return day.weekday() in self._workdays

    def is_working(self, day: date) -> bool:
        return day.weekday() in self._workdays and self._time_off.get(day) != TimeOffPart.FULL

    def next_working_day(self, day: date) -> date:
        """The first working day strictly after `day`."""
        return self._step(day, 1)

    def prev_working_day(self, day: date) -> date:
        """The last working day strictly before `day`."""
        return self._step(day, -1)

    def working_on_or_after(self, day: date) -> date:
        return day if self.is_working(day) else self.next_working_day(day)

    def _step(self, day: date, direction: int) -> date:
        # Every week has a workday, and each full day off can push the search back by at
        # most one week, so this bound is always enough.
        candidate = day
        for _step in range(7 * (len(self._time_off) + 1)):
            candidate += timedelta(days=direction)
            if self.is_working(candidate):
                return candidate
        raise AssertionError("unreachable: a workday exists in every week")

    # Hours ------------------------------------------------------------------------------

    def _full_window(self, day: date, tz: tzinfo) -> Interval:
        return Interval(_at(day, self._work_start, tz), _at(day, self._work_end, tz))

    def _lunch(self, day: date, tz: tzinfo) -> Interval | None:
        if not self._lunch_minutes:
            return None
        start = _at(day, self._lunch_start, tz)
        return Interval(start, start + timedelta(minutes=self._lunch_minutes))

    def _split_point(self, day: date, tz: tzinfo) -> tuple[datetime, datetime]:
        """Where a half day off cuts the day: (end of the morning, start of the afternoon)."""
        lunch = self._lunch(day, tz)
        if lunch is not None:
            return lunch.start, lunch.end
        full = self._full_window(day, tz)
        middle = full.start + (full.end - full.start) / 2
        return middle, middle

    def day_window(self, day: date, tz: tzinfo) -> Interval | None:
        """The working hours of `day`, shortened by half-day leave; None if not working."""
        if not self.is_working(day):
            return None
        full = self._full_window(day, tz)
        part = self._time_off.get(day)
        if part is None:
            return full
        morning_end, afternoon_start = self._split_point(day, tz)
        if part == TimeOffPart.AM:
            return Interval(afternoon_start, full.end)
        return Interval(full.start, morning_end)

    def lunch(self, day: date, tz: tzinfo) -> Interval | None:
        """The lunch break, if it falls inside the day's working window."""
        window = self.day_window(day, tz)
        lunch = self._lunch(day, tz)
        if window is None or lunch is None:
            return None
        if lunch.end <= window.start or lunch.start >= window.end:
            return None
        return lunch

    def leave(self, day: date, tz: tzinfo) -> Interval | None:
        """The part of the normal working hours taken off, for the timeline."""
        if day.weekday() not in self._workdays:
            return None
        part = self._time_off.get(day)
        full = self._full_window(day, tz)
        if part == TimeOffPart.FULL:
            return full
        if part is None:
            return None
        morning_end, afternoon_start = self._split_point(day, tz)
        if part == TimeOffPart.AM:
            return Interval(full.start, morning_end)
        return Interval(afternoon_start, full.end)
