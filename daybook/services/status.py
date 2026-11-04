"""`daybook --status` (D-031) and the tray's text: what the timer is doing, as plain data.

Reads only, so it works on the running instance's connection and on a read-only one
when Daybook isn't running.
"""

from dataclasses import dataclass
from datetime import datetime, tzinfo
from gettext import gettext as _

from daybook.core.db import Connection
from daybook.core.errors import NotFoundError
from daybook.core.repo import plan as plan_repo
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_entries
from daybook.core.traymenu import TrayStatus, tooltip


@dataclass(frozen=True)
class TimerSummary:
    name: str  # "PROJ-123 Fix login"
    running: bool
    left_min: int | None  # of today's allotment; None for Rest of day or unplanned
    tracked_min: int  # today


def timer_summary(conn: Connection, now: datetime, tz: tzinfo) -> TimerSummary | None:
    state = time_entries.timer_state(conn)
    if state is None:
        return None
    try:
        task = tasks_repo.get(conn, state.task_id)
    except NotFoundError:
        return None
    today = now.astimezone(tz).date()
    tracked_min = time_entries.tracked(conn, task.id, today, tz, now).today_s // 60
    item = next((i for i in plan_repo.items_on(conn, today) if i.task_id == task.id), None)
    left = None
    if item is not None and item.allotted_min is not None:
        left = item.allotted_min - tracked_min
    name = f"{task.ticket_key} {task.title}" if task.ticket_key else task.title
    return TimerSummary(name, state.running, left, tracked_min)


def tray_status(conn: Connection, now: datetime, tz: tzinfo) -> TrayStatus:
    summary = timer_summary(conn, now, tz)
    if summary is None:
        return TrayStatus(None, False, None)
    return TrayStatus(summary.name, summary.running, summary.left_min, summary.tracked_min)


def status_line(conn: Connection, now: datetime, tz: tzinfo) -> str:
    """``PROJ-123 Fix login · 23m left · running``, or ``No timer running``."""
    status = tray_status(conn, now, tz)
    return tooltip(status) if status.task is not None else _("No timer running")
