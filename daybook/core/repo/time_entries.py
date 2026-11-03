"""Tracked time: read side only until the timer arrives (Phase 3)."""

from datetime import date, datetime, tzinfo

from daybook.core import timeutil
from daybook.core.db import Connection
from daybook.core.models import Tracked


def _overlap_seconds(start: datetime, end: datetime, lo: datetime, hi: datetime) -> int:
    return max(0, int((min(end, hi) - max(start, lo)).total_seconds()))


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
    live = conn.execute(
        "SELECT segment_start_utc FROM timer_state WHERE task_id = ? AND state = 'running'",
        (task_id,),
    ).fetchone()
    if live is not None:
        segment_start = timeutil.from_db(live["segment_start_utc"])
        if now > segment_start:
            intervals.append((segment_start, now))
    total = sum(int((end - start).total_seconds()) for start, end in intervals)
    today_s = sum(_overlap_seconds(start, end, day_start, day_end) for start, end in intervals)
    return Tracked(today_s=today_s, total_s=total)


def total_seconds(conn: Connection, task_id: int) -> int:
    """Closed time entries only; used to warn before deleting a task (D-017)."""
    row = conn.execute(
        "SELECT coalesce(sum(strftime('%s', end_utc) - strftime('%s', start_utc)), 0) "
        "FROM time_entry WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row[0])
