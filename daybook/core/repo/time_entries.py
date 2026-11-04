"""Time entries: tracked totals, and manual entries added, edited and deleted by hand.

Timer entries are written by `repo/timer.py` when a segment closes. Entries never overlap
(D-023): `core/tracking.py` checks first, and the DB trigger is the backstop.
"""

import sqlite3
from datetime import date, datetime, tzinfo
from gettext import gettext as _

from daybook.core import timeutil, tracking
from daybook.core.db import Connection, transaction
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import (
    EntrySource,
    PauseReason,
    TimeEntry,
    TimerRun,
    TimerState,
    Tracked,
)

OVERLAP_MESSAGE = "time_entry_overlap"  # raised by the m0001 triggers


def _from_row(row: sqlite3.Row) -> TimeEntry:
    return TimeEntry(
        id=row["id"],
        task_id=row["task_id"],
        start=timeutil.from_db(row["start_utc"]),
        end=timeutil.from_db(row["end_utc"]),
        note=row["note"],
        source=EntrySource(row["source"]),
    )


def timer_state(conn: Connection) -> TimerState | None:
    row = conn.execute("SELECT * FROM timer_state WHERE id = 1").fetchone()
    if row is None:
        return None
    segment = row["segment_start_utc"]
    return TimerState(
        task_id=row["task_id"],
        run=TimerRun(row["state"]),
        segment_start=timeutil.from_db(segment) if segment else None,
        last_seen=timeutil.from_db(row["last_seen_utc"]),
        pause_reason=PauseReason(row["pause_reason"]) if row["pause_reason"] else None,
        paused_by_meeting_key=row["paused_by_meeting_key"],
    )


def get(conn: Connection, entry_id: int) -> TimeEntry:
    row = conn.execute("SELECT * FROM time_entry WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise NotFoundError("time_entry", entry_id)
    return _from_row(row)


def for_task(conn: Connection, task_id: int, limit: int = 200) -> list[TimeEntry]:
    """The task's entries, newest first."""
    rows = conn.execute(
        "SELECT * FROM time_entry WHERE task_id = ? ORDER BY start_utc DESC LIMIT ?",
        (task_id, limit),
    )
    return [_from_row(row) for row in rows]


def between(conn: Connection, start: datetime, end: datetime) -> list[TimeEntry]:
    """Entries of any task overlapping [start, end), oldest first."""
    rows = conn.execute(
        "SELECT * FROM time_entry WHERE start_utc < ? AND end_utc > ? ORDER BY start_utc",
        (timeutil.to_db(end), timeutil.to_db(start)),
    )
    return [_from_row(row) for row in rows]


def on_day(conn: Connection, day: date, tz: tzinfo) -> list[TimeEntry]:
    start, end = timeutil.local_day_bounds(day, tz)
    return between(conn, start, end)


def ending_after(conn: Connection, instant: datetime) -> list[TimeEntry]:
    rows = conn.execute(
        "SELECT * FROM time_entry WHERE end_utc > ? ORDER BY start_utc",
        (timeutil.to_db(instant),),
    )
    return [_from_row(row) for row in rows]


def insert(
    conn: Connection,
    task_id: int,
    start: datetime,
    end: datetime,
    *,
    source: EntrySource,
    note: str = "",
    entry_id: int | None = None,
) -> int:
    """One INSERT, for use inside a caller's transaction. Raises ValidationError when the
    overlap trigger fires."""
    try:
        cursor = conn.execute(
            "INSERT INTO time_entry (id, task_id, start_utc, end_utc, note, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, task_id, timeutil.to_db(start), timeutil.to_db(end), note, source.value),
        )
    except sqlite3.IntegrityError as exc:
        if OVERLAP_MESSAGE in str(exc):
            raise ValidationError("start", _("This overlaps time already logged.")) from exc
        raise
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _check_task(conn: Connection, task_id: int) -> None:
    row = conn.execute(
        "SELECT 1 FROM task WHERE id = ? AND deleted_at IS NULL", (task_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("task", task_id)


def add_manual(
    conn: Connection, task_id: int, start: datetime, end: datetime, *, note: str, now: datetime
) -> int:
    with transaction(conn):
        _check_task(conn, task_id)
        tracking.check_entry(start, end, now, between(conn, start, end), timer_state(conn))
        return insert(conn, task_id, start, end, source=EntrySource.MANUAL, note=note.strip())


def update(
    conn: Connection, entry_id: int, start: datetime, end: datetime, *, note: str, now: datetime
) -> TimeEntry:
    """Change an entry's times or note. An edited timer entry becomes a manual one: the
    times are now what the user says, and meeting trimming never touches it (D-090)."""
    with transaction(conn):
        before = get(conn, entry_id)
        _check_task(conn, before.task_id)
        tracking.check_entry(
            start, end, now, between(conn, start, end), timer_state(conn), editing=entry_id
        )
        source = before.source
        if (start, end) != (before.start, before.end):
            source = EntrySource.MANUAL
        try:
            conn.execute(
                "UPDATE time_entry SET start_utc = ?, end_utc = ?, note = ?, source = ? "
                "WHERE id = ?",
                (timeutil.to_db(start), timeutil.to_db(end), note.strip(), source.value, entry_id),
            )
        except sqlite3.IntegrityError as exc:
            if OVERLAP_MESSAGE in str(exc):
                raise ValidationError("start", _("This overlaps time already logged.")) from exc
            raise
        return before


def delete(conn: Connection, entry_id: int) -> TimeEntry:
    with transaction(conn):
        entry = get(conn, entry_id)
        conn.execute("DELETE FROM time_entry WHERE id = ?", (entry_id,))
        return entry


def reinsert(conn: Connection, entry: TimeEntry) -> int:
    """Undo of a delete: log the same time again. It gets a new id, because SQLite may
    have given the old one to a newer entry."""
    with transaction(conn):
        exists = conn.execute("SELECT 1 FROM task WHERE id = ?", (entry.task_id,)).fetchone()
        if exists is None:
            raise NotFoundError("task", entry.task_id)
        return insert(
            conn, entry.task_id, entry.start, entry.end, source=entry.source, note=entry.note
        )


# Totals -------------------------------------------------------------------------------------


def _live(conn: Connection, now: datetime) -> tuple[int, tuple[datetime, datetime]] | None:
    state = timer_state(conn)
    interval = tracking.live_interval(state, now)
    if state is None or interval is None:
        return None
    return state.task_id, interval


def tracked(conn: Connection, task_id: int, today: date, tz: tzinfo, now: datetime) -> Tracked:
    """Time on a task today (clipped to the local day) and in total, including a running
    timer segment, which is not a time entry until it is closed (D-023)."""
    day_start, day_end = timeutil.local_day_bounds(today, tz)
    intervals = [
        (timeutil.from_db(row["start_utc"]), timeutil.from_db(row["end_utc"]))
        for row in conn.execute(
            "SELECT start_utc, end_utc FROM time_entry WHERE task_id = ?", (task_id,)
        )
    ]
    live = _live(conn, now)
    if live is not None and live[0] == task_id:
        intervals.append(live[1])
    total = sum(int((end - start).total_seconds()) for start, end in intervals)
    return Tracked(today_s=tracking.clipped_s(intervals, day_start, day_end), total_s=total)


def total_seconds(conn: Connection, task_id: int) -> int:
    """Closed time entries only; used to warn before deleting a task (D-017)."""
    row = conn.execute(
        "SELECT coalesce(sum(strftime('%s', end_utc) - strftime('%s', start_utc)), 0) "
        "FROM time_entry WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row[0])


def tracked_on_day(
    conn: Connection, task_ids: list[int], day: date, tz: tzinfo, now: datetime
) -> dict[int, int]:
    """Minutes tracked per task on one local day, including a running timer segment."""
    if not task_ids:
        return {}
    start, end = timeutil.local_day_bounds(day, tz)
    marks = ", ".join("?" * len(task_ids))
    seconds: dict[int, int] = dict.fromkeys(task_ids, 0)
    rows = conn.execute(
        f"SELECT task_id, start_utc, end_utc FROM time_entry WHERE task_id IN ({marks}) "
        "AND start_utc < ? AND end_utc > ?",
        (*task_ids, timeutil.to_db(end), timeutil.to_db(start)),
    )
    for row in rows:
        seconds[row["task_id"]] += tracking.overlap_s(
            timeutil.from_db(row["start_utc"]), timeutil.from_db(row["end_utc"]), start, end
        )
    live = _live(conn, now)
    if live is not None and live[0] in seconds:
        seconds[live[0]] += tracking.overlap_s(*live[1], start, end)
    return {task_id: value // 60 for task_id, value in seconds.items()}
