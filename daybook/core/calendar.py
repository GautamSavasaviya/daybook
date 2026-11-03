"""WorkCalendar: the only code that decides which days are working days (CLAUDE.md).

It knows the workdays setting and time off (D-048). A full day off is not a working day;
a half day off still is (the planner just gets less time, Phase 2).
"""

from collections.abc import Mapping
from datetime import date, timedelta
from types import MappingProxyType

from daybook.core.models import TimeOffPart
from daybook.core.settings import Settings

_NO_TIME_OFF: Mapping[date, TimeOffPart] = MappingProxyType({})


class WorkCalendar:
    def __init__(
        self, workdays: frozenset[int], time_off: Mapping[date, TimeOffPart] = _NO_TIME_OFF
    ) -> None:
        if not workdays:
            raise ValueError("at least one workday is required")
        self._workdays = workdays
        self._time_off = MappingProxyType(dict(time_off))

    @classmethod
    def from_settings(
        cls, settings: Settings, time_off: Mapping[date, TimeOffPart] = _NO_TIME_OFF
    ) -> WorkCalendar:
        return cls(settings.workdays, time_off)

    def time_off(self, day: date) -> TimeOffPart | None:
        return self._time_off.get(day)

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
