"""The timer state machine, recovery and meeting trimming (PLAN §5.6). Pure: no clock, no I/O.

    no timer ──start──▶ running ⇄ paused ──stop──▶ no timer

Every transition that ends a running segment hands back the `Closed` stretch, which the
repository writes as one `time_entry(source='timer')` in the same transaction as the new
state (D-023). A segment shorter than a second is dropped.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from gettext import gettext as _

from daybook.core.errors import ValidationError
from daybook.core.models import EntrySource, PauseReason, TimeEntry, TimerRun, TimerState

log = logging.getLogger(__name__)

MIN_SEGMENT = timedelta(seconds=1)


@dataclass(frozen=True)
class Closed:
    """A finished stretch of timer time, to be stored as a time entry."""

    task_id: int
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Transition:
    closed: Closed | None
    state: TimerState | None  # None: no timer


def close(state: TimerState | None, end: datetime) -> Closed | None:
    """The running segment up to `end`, or None when there is nothing worth keeping."""
    if state is None or state.segment_start is None:
        return None
    if end - state.segment_start < MIN_SEGMENT:
        if end < state.segment_start:
            # The wall clock went backwards past the segment start (D-023).
            log.warning(
                "dropping a timer segment that ends before it starts (%s → %s)",
                state.segment_start,
                end,
            )
        return None
    return Closed(state.task_id, state.segment_start, end)


def start(state: TimerState | None, task_id: int, now: datetime) -> Transition:
    """Run the timer on `task_id`. Another task's segment is closed first (one timer)."""
    if state is not None and state.running and state.task_id == task_id:
        return Transition(None, heartbeat(state, now))
    return Transition(close(state, now), TimerState(task_id, TimerRun.RUNNING, now, now))


def pause(
    state: TimerState | None,
    now: datetime,
    reason: PauseReason = PauseReason.USER,
    meeting_key: str | None = None,
) -> Transition:
    if state is None or not state.running:
        return Transition(None, state)
    paused = TimerState(state.task_id, TimerRun.PAUSED, None, now, reason, meeting_key)
    return Transition(close(state, now), paused)


def resume(state: TimerState | None, now: datetime) -> Transition:
    if state is None or state.running:
        return Transition(None, state)
    return Transition(None, TimerState(state.task_id, TimerRun.RUNNING, now, now))


def stop(state: TimerState | None, now: datetime) -> Transition:
    return Transition(close(state, now), None)


def heartbeat(state: TimerState, now: datetime) -> TimerState:
    return replace(state, last_seen=now)


def elapsed(state: TimerState | None, now: datetime) -> timedelta:
    """Length of the live segment (zero when paused or stopped)."""
    if state is None or state.segment_start is None:
        return timedelta()
    return max(timedelta(), now - state.segment_start)


# Recovery ---------------------------------------------------------------------------------


class RecoveryChoice(StrEnum):
    KEEP = "keep"  # the segment continues
    STOP_AT_LAST_SEEN = "stop"  # write it up to the last heartbeat, stop
    DISCARD = "discard"  # drop the segment, stop


@dataclass(frozen=True)
class Recovery:
    """A timer found running at startup: the app did not shut down cleanly (PLAN §5.6)."""

    task_id: int
    since: datetime
    last_seen: datetime


def recovery(state: TimerState | None) -> Recovery | None:
    if state is None or state.segment_start is None:
        return None
    return Recovery(state.task_id, state.segment_start, state.last_seen)


def recover(state: TimerState, choice: RecoveryChoice, now: datetime) -> Transition:
    match choice:
        case RecoveryChoice.KEEP:
            return Transition(None, heartbeat(state, now))
        case RecoveryChoice.STOP_AT_LAST_SEEN:
            return Transition(close(state, state.last_seen), None)
        case RecoveryChoice.DISCARD:
            return Transition(None, None)


# Meeting trimming ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trim:
    """What starting a meeting at `cut` (possibly in the past) does to tracked time (D-049):
    timer entries after the cut are shortened or deleted and a running timer pauses."""

    cut: datetime
    shorten: tuple[TimeEntry, ...]  # each ends at `cut` afterwards
    delete: tuple[TimeEntry, ...]
    transition: Transition  # the timer's new state, and the live time kept before the cut
    removed_s: dict[int, int]  # task id → seconds removed

    @property
    def removed_total_s(self) -> int:
        return sum(self.removed_s.values())


def trim_for_meeting(
    entries: Sequence[TimeEntry],
    state: TimerState | None,
    cut: datetime,
    now: datetime,
    meeting_key: str | None = None,
) -> Trim:
    """Plan the trim for a meeting that really started at `cut` (≤ `now`).

    `entries` are those ending after `cut`. Manual entries are never touched: if one
    reaches past the cut, the start is refused (ValidationError on "started").
    """
    shorten: list[TimeEntry] = []
    delete: list[TimeEntry] = []
    removed: dict[int, int] = {}

    def remove(task_id: int, seconds: float) -> None:
        if seconds > 0:
            removed[task_id] = removed.get(task_id, 0) + int(seconds)

    for entry in entries:
        if entry.end <= cut:
            continue
        if entry.source == EntrySource.MANUAL:
            raise ValidationError(
                "started",
                _(
                    "Time you entered by hand overlaps the meeting's start. Edit that "
                    "entry, or choose a later start."
                ),
            )
        if entry.start >= cut:
            delete.append(entry)
            remove(entry.task_id, entry.seconds)
        else:
            shorten.append(entry)
            remove(entry.task_id, (entry.end - cut).total_seconds())

    if state is not None and state.segment_start is not None:
        kept_until = max(cut, state.segment_start)
        remove(state.task_id, (now - kept_until).total_seconds())
        closed = close(state, cut) if state.segment_start < cut else None
        paused = TimerState(
            state.task_id, TimerRun.PAUSED, None, now, PauseReason.MEETING, meeting_key
        )
        transition = Transition(closed, paused)
    else:
        transition = Transition(None, state)
    return Trim(cut, tuple(shorten), tuple(delete), transition, removed)
