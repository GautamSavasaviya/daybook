"""Recurrence rules for meetings and tasks (PLAN §4, §5.4).

Occurrences are computed in local wall-clock time and then placed in UTC, so a 10:00
standup stays at 10:00 across DST (D-010). A time that doesn't exist (spring forward)
moves forward by the gap; a repeated time (fall back) takes the first one
(`timeutil.localize`). Monthly rules on days 29 to 31 fall on the month's last day in
shorter months instead of skipping them (D-011).
"""

import calendar as _monthcal
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, tzinfo
from gettext import gettext as _

from daybook.core.errors import ValidationError
from daybook.core.models import Recurrence, RecurrenceFreq
from daybook.core.timeutil import date_from_db, date_to_db, fmt_day_month, localize

MAX_INTERVAL = 99
# How far `next_date` looks ahead; a monthly rule every 99 months still fits.
_SEARCH_LIMIT_DAYS = 31 * MAX_INTERVAL + 31


# --- (De)serialisation ------------------------------------------------------------------


def to_json(rule: Recurrence) -> str:
    data: dict[str, object] = {"freq": rule.freq.value, "interval": rule.interval}
    if rule.freq == RecurrenceFreq.WEEKLY and rule.days:
        data["days"] = sorted(rule.days)
    if rule.until is not None:
        data["until"] = date_to_db(rule.until)
    return json.dumps(data, sort_keys=True)


def from_json(text: str | None) -> Recurrence | None:
    """Parse a stored rule. Raises ValueError for anything malformed."""
    if text is None:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("recurrence must be an object")
    interval = data.get("interval", 1)
    days = data.get("days", [])
    until = data.get("until")
    if not isinstance(interval, int) or not isinstance(days, list):
        raise ValueError("bad recurrence fields")
    return validate(
        Recurrence(
            freq=RecurrenceFreq(data["freq"]),
            interval=interval,
            days=frozenset(int(day) for day in days),
            until=date_from_db(until) if isinstance(until, str) else None,
        )
    )


def validate(rule: Recurrence, field: str = "recurrence") -> Recurrence:
    if not 1 <= rule.interval <= MAX_INTERVAL:
        raise ValidationError(field, _("Repeat every 1 to {n}.").format(n=MAX_INTERVAL))
    if not rule.days <= frozenset(range(7)):
        raise ValidationError(field, _("Weekdays must be Monday to Sunday."))
    days = rule.days if rule.freq == RecurrenceFreq.WEEKLY else frozenset()
    interval = 1 if rule.freq == RecurrenceFreq.WEEKDAYS else rule.interval
    return Recurrence(rule.freq, interval, days, rule.until)


# --- Expansion --------------------------------------------------------------------------


def _months(day: date) -> int:
    return day.year * 12 + day.month - 1


def _monthly(anchor: date, months_from_anchor: int) -> date:
    total = _months(anchor) + months_from_anchor
    year, month = divmod(total, 12)
    last = _monthcal.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(anchor.day, last))


def dates(rule: Recurrence, anchor: date, first: date, last: date) -> Iterator[date]:
    """Occurrence dates of a rule starting on `anchor`, within [first, last] (inclusive)."""
    first = max(first, anchor)
    if rule.until is not None:
        last = min(last, rule.until)
    if first > last:
        return
    match rule.freq:
        case RecurrenceFreq.MONTHLY:
            step = rule.interval
            index = max(0, (_months(first) - _months(anchor)) // step * step - step)
            while True:
                candidate = _monthly(anchor, index)
                if candidate > last:
                    return
                if candidate >= first:
                    yield candidate
                index += step
        case _:
            weekdays = rule.days or frozenset({anchor.weekday()})
            anchor_monday = anchor - timedelta(days=anchor.weekday())
            day = first
            while day <= last:
                if _matches(rule, anchor, anchor_monday, weekdays, day):
                    yield day
                day += timedelta(days=1)


def _matches(
    rule: Recurrence, anchor: date, anchor_monday: date, weekdays: frozenset[int], day: date
) -> bool:
    match rule.freq:
        case RecurrenceFreq.DAILY:
            return (day - anchor).days % rule.interval == 0
        case RecurrenceFreq.WEEKDAYS:
            return day.weekday() < 5
        case _:  # WEEKLY
            week = (day - timedelta(days=day.weekday()) - anchor_monday).days // 7
            return week % rule.interval == 0 and day.weekday() in weekdays


def starts(
    rule: Recurrence | None,
    anchor: datetime,
    tz: tzinfo,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """UTC start times of a series anchored at `anchor` (UTC) that fall in
    [window_start, window_end). A one-off (no rule) has just its anchor."""
    if rule is None:
        return [anchor] if window_start <= anchor < window_end else []
    local_anchor = anchor.astimezone(tz)
    wall_clock = local_anchor.time().replace(tzinfo=None)
    # One day of margin on both sides: the local date of a UTC instant can differ by one.
    first = window_start.astimezone(tz).date() - timedelta(days=1)
    last = window_end.astimezone(tz).date() + timedelta(days=1)
    result: list[datetime] = []
    for day in dates(rule, local_anchor.date(), first, last):
        start = localize(datetime.combine(day, wall_clock), tz).astimezone(UTC)
        if window_start <= start < window_end and start >= anchor:
            result.append(start)
    return result


def next_date(rule: Recurrence, anchor: date, after: date) -> date | None:
    """The first occurrence date strictly after `after`, or None once `until` has passed.
    Used for recurring tasks (D-016)."""
    first = after + timedelta(days=1)
    return next(dates(rule, anchor, first, first + timedelta(days=_SEARCH_LIMIT_DAYS)), None)


# --- Display ----------------------------------------------------------------------------


def weekday_names() -> tuple[str, ...]:
    return (_("Mon"), _("Tue"), _("Wed"), _("Thu"), _("Fri"), _("Sat"), _("Sun"))


def describe(rule: Recurrence | None, anchor: date) -> str:
    """``Weekdays``, ``Every 2 weeks on Mon, Wed``, ``Monthly on day 31``…"""
    if rule is None:
        return _("Doesn't repeat")
    n = rule.interval
    match rule.freq:
        case RecurrenceFreq.DAILY:
            text = _("Daily") if n == 1 else _("Every {n} days").format(n=n)
        case RecurrenceFreq.WEEKDAYS:
            text = _("Weekdays")
        case RecurrenceFreq.WEEKLY:
            names = weekday_names()
            days = ", ".join(names[d] for d in sorted(rule.days or {anchor.weekday()}))
            if n == 1:
                text = _("Weekly on {days}").format(days=days)
            else:
                text = _("Every {n} weeks on {days}").format(n=n, days=days)
        case RecurrenceFreq.MONTHLY:
            if n == 1:
                text = _("Monthly on day {day}").format(day=anchor.day)
            else:
                text = _("Every {n} months on day {day}").format(n=n, day=anchor.day)
    if rule.until is not None:
        text += " " + _("until {day}").format(day=fmt_day_month(rule.until))
    return text
