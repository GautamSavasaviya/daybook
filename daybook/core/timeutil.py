"""Time conversion and formatting.

Storage format (D-009): instants are fixed-width UTC text ``YYYY-MM-DDTHH:MM:SSZ`` so
string comparison is chronological; local calendar dates are ``YYYY-MM-DD``. Only this
module converts between those strings and datetimes.
"""

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from gettext import gettext as _

DB_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_DB_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DB_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def to_db(instant: datetime) -> str:
    """Serialise an aware datetime as UTC, truncated to whole seconds."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("naive datetime cannot be stored; attach a timezone first")
    return instant.astimezone(UTC).strftime(DB_FORMAT)


def from_db(text: str) -> datetime:
    if not _DB_INSTANT.fullmatch(text):
        raise ValueError(f"not a stored instant: {text!r}")
    # fromisoformat reads the trailing "Z" as UTC and is ~15x faster than strptime, which
    # matters when a view reads tens of thousands of stored times.
    return datetime.fromisoformat(text).astimezone(UTC)


def date_to_db(day: date) -> str:
    return day.isoformat()


def date_from_db(text: str) -> date:
    if not _DB_DATE.fullmatch(text):
        raise ValueError(f"not a stored date: {text!r}")
    return date.fromisoformat(text)


def localize(naive: datetime, tz: tzinfo) -> datetime:
    """Attach `tz` to a local wall-clock time, resolving DST edge cases deterministically.

    Ambiguous times (the repeated hour in autumn) take the first occurrence (fold=0).
    Nonexistent times (the skipped hour in spring) move forward by the length of the gap,
    so 02:30 on a spring-forward night becomes 03:30.
    """
    if naive.tzinfo is not None:
        raise ValueError("localize() expects a naive datetime")
    candidate = naive.replace(tzinfo=tz, fold=0)
    # Round-tripping through UTC normalises a nonexistent wall time to the real instant.
    return candidate.astimezone(UTC).astimezone(tz)


def local_date(instant: datetime, tz: tzinfo) -> date:
    return instant.astimezone(tz).date()


def local_day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """UTC instants of local midnight at the start of `day` and of the next day."""
    start = localize(datetime.combine(day, time()), tz)
    end = localize(datetime.combine(day + timedelta(days=1), time()), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def fmt_duration(minutes: int) -> str:
    """Human-friendly duration: ``45m``, ``1h 05m``, ``-15m`` for overruns."""
    sign = "-" if minutes < 0 else ""
    hours, mins = divmod(abs(minutes), 60)
    if hours == 0:
        return f"{sign}{mins}m"
    return f"{sign}{hours}h {mins:02d}m"


def fmt_relative(delta: timedelta) -> str:
    """``in 12 min``, ``12 min ago``, ``in 1h 05m`` or ``now`` (under half a minute)."""
    minutes = int(abs(delta.total_seconds()) / 60 + 0.5)  # halves round up, not to even
    if minutes == 0:
        return _("now")
    amount = _("{minutes} min").format(minutes=minutes) if minutes < 60 else fmt_duration(minutes)
    if delta.total_seconds() > 0:
        return _("in {amount}").format(amount=amount)
    return _("{amount} ago").format(amount=amount)


def fmt_clock(instant: datetime, tz: tzinfo, *, use_24h: bool) -> str:
    """Local wall-clock time, ``14:05`` or ``2:05 PM`` (follows GNOME's clock-format, D-034)."""
    local = instant.astimezone(tz)
    if use_24h:
        return f"{local.hour:02d}:{local.minute:02d}"
    hour12 = local.hour % 12 or 12
    suffix = _("AM") if local.hour < 12 else _("PM")
    return f"{hour12}:{local.minute:02d} {suffix}"


def fmt_day(day: date, today: date) -> str:
    """A calendar date relative to `today`: ``today``, ``tomorrow``, ``yesterday``, the weekday
    within the coming week (``Fri``), otherwise ``Wed 4 Nov`` (``Wed 4 Nov 2027`` in another
    year). Lower-case words, so callers can embed them ("due tomorrow")."""
    delta = (day - today).days
    if delta == 0:
        return _("today")
    if delta == 1:
        return _("tomorrow")
    if delta == -1:
        return _("yesterday")
    weekday = _WEEKDAYS[day.weekday()]()
    if 1 < delta < 7:
        return weekday
    if day.year == today.year:
        return f"{weekday} {fmt_day_month(day)}"
    return f"{weekday} {fmt_day_month(day)} {day.year}"


def fmt_day_month(day: date) -> str:
    """``3 Nov``."""
    return f"{day.day} {_MONTHS[day.month - 1]()}"


def fmt_weekday(day: date) -> str:
    """The short weekday name, ``Wed``."""
    return _WEEKDAYS[day.weekday()]()


def fmt_date(day: date) -> str:
    """An absolute date, ``Wed 4 Nov 2026``."""
    return f"{_WEEKDAYS[day.weekday()]()} {fmt_day_month(day)} {day.year}"


# Functions, so the names are translated when used rather than at import time.
_WEEKDAYS = (
    lambda: _("Mon"),
    lambda: _("Tue"),
    lambda: _("Wed"),
    lambda: _("Thu"),
    lambda: _("Fri"),
    lambda: _("Sat"),
    lambda: _("Sun"),
)
_MONTHS = (
    lambda: _("Jan"),
    lambda: _("Feb"),
    lambda: _("Mar"),
    lambda: _("Apr"),
    lambda: _("May"),
    lambda: _("Jun"),
    lambda: _("Jul"),
    lambda: _("Aug"),
    lambda: _("Sep"),
    lambda: _("Oct"),
    lambda: _("Nov"),
    lambda: _("Dec"),
)
