"""Read-only queries behind the review, standup, timesheet and reports. They take any
connection, so the worker thread can run them on its own (PLAN §2)."""

from dataclasses import dataclass
from datetime import date, datetime, tzinfo

from daybook.core import meetings as meeting_rules
from daybook.core import timeutil, tracking
from daybook.core.db import Connection
from daybook.core.models import Occurrence, TimerState, TimeSlice
from daybook.core.repo import meetings as meetings_repo
from daybook.core.repo import time_entries
from daybook.core.reports import PlannedItem

_SLICE_SELECT = (
    "SELECT e.task_id, t.ticket_key, t.title, t.project_id, p.name AS project_name, "
    "e.start_utc, e.end_utc FROM time_entry e JOIN task t ON t.id = e.task_id "
    "JOIN project p ON p.id = t.project_id "
)


def slices(conn: Connection, start: datetime, end: datetime, now: datetime) -> list[TimeSlice]:
    """Tracked time overlapping [start, end), the running timer's segment included."""
    rows = conn.execute(
        _SLICE_SELECT
        + "WHERE e.start_utc < ? AND e.end_utc > ? AND t.deleted_at IS NULL ORDER BY e.start_utc",
        (timeutil.to_db(end), timeutil.to_db(start)),
    )
    result = [
        TimeSlice(
            r["task_id"],
            r["ticket_key"],
            r["title"],
            r["project_id"],
            r["project_name"],
            timeutil.from_db(r["start_utc"]),
            timeutil.from_db(r["end_utc"]),
        )
        for r in rows
    ]
    state = time_entries.timer_state(conn)
    live = tracking.live_interval(state, now)
    if state is not None and live is not None and live[0] < end and live[1] > start:
        result.append(_live_slice(conn, state, live))
    return result


def _live_slice(conn: Connection, state: TimerState, live: tuple[datetime, datetime]) -> TimeSlice:
    row = conn.execute(
        "SELECT t.ticket_key, t.title, t.project_id, p.name AS project_name FROM task t "
        "JOIN project p ON p.id = t.project_id WHERE t.id = ?",
        (state.task_id,),
    ).fetchone()
    return TimeSlice(
        state.task_id,
        row["ticket_key"],
        row["title"],
        row["project_id"],
        row["project_name"],
        live[0],
        live[1],
    )


@dataclass(frozen=True)
class DoneTask:
    task_id: int
    name: str
    requested_by: str | None


def _name(ticket_key: str | None, title: str) -> str:
    return f"{ticket_key} {title}" if ticket_key else title


def completed_between(conn: Connection, start: datetime, end: datetime) -> list[DoneTask]:
    rows = conn.execute(
        "SELECT id, ticket_key, title, requested_by FROM task WHERE status = 'done' "
        "AND deleted_at IS NULL AND completed_at >= ? AND completed_at < ? "
        "ORDER BY completed_at",
        (timeutil.to_db(start), timeutil.to_db(end)),
    )
    return [DoneTask(r["id"], _name(r["ticket_key"], r["title"]), r["requested_by"]) for r in rows]


@dataclass(frozen=True)
class HeldTask:
    task_id: int
    name: str
    reason: str
    note: str | None
    since: datetime


def on_hold(conn: Connection) -> list[HeldTask]:
    """On-hold tasks, longest waiting first (the standup's Blockers)."""
    rows = conn.execute(
        "SELECT id, ticket_key, title, hold_reason, hold_note, hold_since FROM task "
        "WHERE status = 'on_hold' AND deleted_at IS NULL ORDER BY hold_since, id"
    )
    return [
        HeldTask(
            r["id"],
            _name(r["ticket_key"], r["title"]),
            r["hold_reason"],
            r["hold_note"],
            timeutil.from_db(r["hold_since"]),
        )
        for r in rows
    ]


def planned_between(conn: Connection, first: date, last: date) -> list[PlannedItem]:
    rows = conn.execute(
        "SELECT i.plan_date, i.task_id, i.allotted_min, i.carried_from IS NOT NULL AS carried, "
        "t.ticket_key, t.title FROM day_plan_item i JOIN task t ON t.id = i.task_id "
        "WHERE i.plan_date BETWEEN ? AND ? AND t.deleted_at IS NULL "
        "ORDER BY i.plan_date, i.position",
        (timeutil.date_to_db(first), timeutil.date_to_db(last)),
    )
    return [
        PlannedItem(
            timeutil.date_from_db(r["plan_date"]),
            r["task_id"],
            _name(r["ticket_key"], r["title"]),
            r["allotted_min"],
            bool(r["carried"]),
        )
        for r in rows
    ]


def occurrences_between(
    conn: Connection, start: datetime, end: datetime, fallback: tzinfo
) -> list[Occurrence]:
    """Meeting occurrences that start in [start, end), single changes applied."""
    changes = meetings_repo.exceptions(conn)
    result: list[Occurrence] = []
    for meeting in meetings_repo.active(conn):
        result += meeting_rules.occurrences(
            meeting, changes.get(meeting.id, []), start, end, fallback
        )
    return result


def all_time_entries(conn: Connection) -> list[dict[str, object]]:
    """Every time entry with its task and project, oldest first (for the CSV export)."""
    rows = conn.execute(
        "SELECT e.start_utc, e.end_utc, e.source, e.note, t.ticket_key, t.title, "
        "p.name AS project_name FROM time_entry e JOIN task t ON t.id = e.task_id "
        "JOIN project p ON p.id = t.project_id WHERE t.deleted_at IS NULL ORDER BY e.start_utc"
    )
    return [dict(row) for row in rows]
