"""The live timer: every transition is one transaction that writes the closed segment as a
time entry and the new `timer_state` row (PLAN §5.6, D-023)."""

import logging
from datetime import datetime
from gettext import gettext as _

from daybook.core import timer as rules
from daybook.core import timeutil
from daybook.core.db import Connection, transaction
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import EntrySource, PauseReason, TaskStatus, TimerState
from daybook.core.repo import time_entries

log = logging.getLogger(__name__)


def get(conn: Connection) -> TimerState | None:
    return time_entries.timer_state(conn)


def _write(conn: Connection, state: TimerState | None, now: datetime) -> None:
    if state is None:
        conn.execute("DELETE FROM timer_state WHERE id = 1")
        return
    conn.execute(
        "INSERT INTO timer_state (id, task_id, state, segment_start_utc, last_seen_utc, "
        "pause_reason, paused_by_meeting_key, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET task_id = excluded.task_id, state = excluded.state, "
        "segment_start_utc = excluded.segment_start_utc, "
        "last_seen_utc = excluded.last_seen_utc, pause_reason = excluded.pause_reason, "
        "paused_by_meeting_key = excluded.paused_by_meeting_key, "
        "updated_at = excluded.updated_at",
        (
            state.task_id,
            state.run.value,
            timeutil.to_db(state.segment_start) if state.segment_start else None,
            timeutil.to_db(state.last_seen),
            state.pause_reason.value if state.pause_reason else None,
            state.paused_by_meeting_key,
            timeutil.to_db(now),
        ),
    )


def _store(conn: Connection, transition: rules.Transition, now: datetime) -> None:
    closed = transition.closed
    if closed is not None:
        try:
            time_entries.insert(
                conn, closed.task_id, closed.start, closed.end, source=EntrySource.TIMER
            )
        except ValidationError:
            # Can only happen if the clock jumped back over logged time. Losing this one
            # segment is better than a timer that can never be stopped; the log keeps it.
            log.warning("dropped a timer segment overlapping logged time: %s", closed)
    _write(conn, transition.state, now)


def _open_task_status(conn: Connection, task_id: int) -> TaskStatus:
    row = conn.execute(
        "SELECT status FROM task WHERE id = ? AND deleted_at IS NULL", (task_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("task", task_id)
    status = TaskStatus(row["status"])
    if status == TaskStatus.DONE:
        raise ValidationError("timer", _("Reopen the task to track more time on it."))
    return status


def start(conn: Connection, task_id: int, now: datetime) -> TimerState | None:
    """Run the timer on a task, stopping any other. A To do or On hold task becomes In
    progress (D-046; the caller asks before resuming an on-hold one). Returns the timer
    state from before, so the caller knows which task it was taken from."""
    with transaction(conn):
        status = _open_task_status(conn, task_id)
        before = get(conn)
        _store(conn, rules.start(before, task_id, now), now)
        if status in (TaskStatus.TO_DO, TaskStatus.ON_HOLD):
            conn.execute(
                "UPDATE task SET status = 'in_progress', hold_reason = NULL, hold_note = NULL, "
                "hold_since = NULL, follow_up_date = NULL, updated_at = ? WHERE id = ?",
                (timeutil.to_db(now), task_id),
            )
        return before


def pause(conn: Connection, now: datetime, reason: PauseReason = PauseReason.USER) -> None:
    with transaction(conn):
        _store(conn, rules.pause(get(conn), now, reason), now)


def resume(conn: Connection, now: datetime) -> None:
    with transaction(conn):
        state = get(conn)
        if state is None:
            return
        _open_task_status(conn, state.task_id)
        _store(conn, rules.resume(state, now), now)


def stop(conn: Connection, now: datetime) -> None:
    with transaction(conn):
        _store(conn, rules.stop(get(conn), now), now)


def stop_on(conn: Connection, task_id: int, now: datetime) -> bool:
    """Stop the timer if it is on `task_id`: the task was put on hold, completed or deleted
    (D-046). For use inside the caller's transaction. Returns True if it stopped."""
    with transaction(conn):
        state = get(conn)
        if state is None or state.task_id != task_id:
            return False
        _store(conn, rules.stop(state, now), now)
        return True


def heartbeat(conn: Connection, now: datetime) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE timer_state SET last_seen_utc = ? WHERE id = 1 AND state = 'running'",
            (timeutil.to_db(now),),
        )


def recover(conn: Connection, choice: rules.RecoveryChoice, now: datetime) -> None:
    with transaction(conn):
        state = get(conn)
        if state is None or not state.running:
            return
        _store(conn, rules.recover(state, choice, now), now)


# Meetings -----------------------------------------------------------------------------------


def plan_trim(
    conn: Connection, cut: datetime, now: datetime, meeting_key: str | None = None
) -> rules.Trim:
    """What a meeting that started at `cut` would do to tracked time (read only)."""
    return rules.trim_for_meeting(
        time_entries.ending_after(conn, cut), get(conn), cut, now, meeting_key
    )


def apply_trim(conn: Connection, cut: datetime, now: datetime, meeting_key: str) -> rules.Trim:
    """Pause a running timer for a meeting and remove timer time after its real start
    (D-049). Raises ValidationError if a manual entry is in the way."""
    with transaction(conn):
        trim = plan_trim(conn, cut, now, meeting_key)
        for entry in trim.delete:
            conn.execute("DELETE FROM time_entry WHERE id = ?", (entry.id,))
        for entry in trim.shorten:
            conn.execute(
                "UPDATE time_entry SET end_utc = ? WHERE id = ?",
                (timeutil.to_db(cut), entry.id),
            )
        _store(conn, trim.transition, now)
        return trim
