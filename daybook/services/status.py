"""`daybook --status` (D-031): one line about the timer, for shell prompts and scripts.

Reads only, so it works on the running instance's connection and on a read-only one
when Daybook isn't running.
"""

from datetime import datetime, tzinfo
from gettext import gettext as _

from daybook.core.db import Connection
from daybook.core.errors import NotFoundError
from daybook.core.repo import plan as plan_repo
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_entries
from daybook.core.timeutil import fmt_duration


def status_line(conn: Connection, now: datetime, tz: tzinfo) -> str:
    """``PROJ-123 Fix login · 23m left · running``, or ``No timer running``."""
    state = time_entries.timer_state(conn)
    if state is None:
        return _("No timer running")
    try:
        task = tasks_repo.get(conn, state.task_id)
    except NotFoundError:
        return _("No timer running")
    today = now.astimezone(tz).date()
    tracked_min = time_entries.tracked(conn, task.id, today, tz, now).today_s // 60
    item = next((i for i in plan_repo.items_on(conn, today) if i.task_id == task.id), None)
    if item is not None and item.allotted_min is not None:
        left = item.allotted_min - tracked_min
        amount = (
            _("{time} left").format(time=fmt_duration(left))
            if left >= 0
            else _("{time} over").format(time=fmt_duration(-left))
        )
    else:
        amount = _("{time} today").format(time=fmt_duration(tracked_min))
    name = f"{task.ticket_key} {task.title}" if task.ticket_key else task.title
    run = _("running") if state.running else _("paused")
    return f"{name} · {amount} · {run}"
