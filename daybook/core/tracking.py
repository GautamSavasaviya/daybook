"""Tracked time: day clipping and the rules for manual entries (PLAN §5.6, D-023). Pure."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from gettext import gettext as _

from daybook.core import timeutil
from daybook.core.errors import OverlapError, ValidationError
from daybook.core.models import TimeEntry, TimerState

# A single manual entry longer than this is almost certainly a typo.
MAX_ENTRY = timedelta(hours=16)


def overlap_s(start: datetime, end: datetime, lo: datetime, hi: datetime) -> int:
    """Whole seconds of [start, end) inside [lo, hi)."""
    return max(0, int((min(end, hi) - max(start, lo)).total_seconds()))


def clipped_s(intervals: Iterable[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> int:
    return sum(overlap_s(start, end, lo, hi) for start, end in intervals)


def live_interval(state: TimerState | None, now: datetime) -> tuple[datetime, datetime] | None:
    """The running segment as an interval, if it has any length yet."""
    if state is None or state.segment_start is None or now <= state.segment_start:
        return None
    return state.segment_start, now


@dataclass(frozen=True)
class EntryInput:
    """A manual entry as typed: a local day, start and end times, a note."""

    day: date
    start: time
    end: time
    note: str = ""


def to_interval(typed: EntryInput, tz: tzinfo) -> tuple[datetime, datetime]:
    """UTC instants for a typed entry. An end at or before the start is an error, except
    an end of exactly midnight (00:00), which means the end of that day."""
    start = timeutil.localize(datetime.combine(typed.day, typed.start), tz)
    end_day = typed.day + timedelta(days=1) if typed.end == time(0) else typed.day
    end = timeutil.localize(datetime.combine(end_day, typed.end), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def check_entry(
    start: datetime,
    end: datetime,
    now: datetime,
    others: Sequence[TimeEntry],
    timer: TimerState | None,
    *,
    editing: int | None = None,
) -> None:
    """Refuse an entry that ends before it starts, ends in the future or overlaps anything
    already tracked, including the running timer (D-023). `others` are the entries near
    [start, end); `editing` is the entry being changed, which may overlap its old self."""
    if end <= start:
        raise ValidationError("end", _("The end must be after the start."))
    if end > now:
        raise ValidationError("end", _("Time can only be logged up to now."))
    if end - start > MAX_ENTRY:
        raise ValidationError("end", _("An entry can be at most 16 hours long."))
    for other in others:
        if other.id != editing and other.start < end and start < other.end:
            raise OverlapError(
                "start",
                _("This overlaps time already logged."),
                start=other.start,
                end=other.end,
                task_id=other.task_id,
                entry_id=other.id,
            )
    live = live_interval(timer, now)
    if timer is not None and live is not None and live[0] < end and start < live[1]:
        raise OverlapError(
            "start",
            _("This overlaps the running timer."),
            start=live[0],
            end=live[1],
            task_id=timer.task_id,
            entry_id=None,
        )


@dataclass(frozen=True)
class Run:
    """Consecutive tracked time on one task, as the Today timeline shows it: pauses and
    resumes on the same task merge into one block."""

    task_id: int
    start: datetime
    end: datetime
    seconds: int
    live: bool  # ends with the running timer


def runs(
    entries: Sequence[TimeEntry],
    timer: TimerState | None,
    lo: datetime,
    hi: datetime,
    now: datetime,
) -> list[Run]:
    """Entries (and the live segment) inside [lo, hi), merged per task while no other task's
    time comes between them."""
    pieces = [(e.task_id, max(e.start, lo), min(e.end, hi), False) for e in entries]
    live = live_interval(timer, now)
    if timer is not None and live is not None:
        pieces.append((timer.task_id, max(live[0], lo), min(live[1], hi), True))
    pieces = [p for p in pieces if p[2] > p[1]]
    pieces.sort(key=lambda p: p[1])
    result: list[Run] = []
    for task_id, start, end, is_live in pieces:
        seconds = int((end - start).total_seconds())
        last = result[-1] if result else None
        if last is not None and last.task_id == task_id:
            result[-1] = Run(task_id, last.start, end, last.seconds + seconds, is_live)
        else:
            result.append(Run(task_id, start, end, seconds, is_live))
    return result
