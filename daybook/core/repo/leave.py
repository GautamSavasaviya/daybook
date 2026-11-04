"""Marking time off, which moves full days' plans to the next working day (D-048).

Both directions are one transaction: the leave rows and the plan moves succeed or fail
together.
"""

from collections.abc import Callable, Sequence
from datetime import date

from daybook.core.db import Connection, transaction
from daybook.core.models import ACTIVE_STATUSES, TimeOff, TimeOffPart
from daybook.core.repo import plan as plan_repo
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_off as time_off_repo


def mark(
    conn: Connection,
    entries: Sequence[TimeOff],
    target_for: Callable[[date], date],
    today: date,
) -> list[tasks_repo.TaskState]:
    """Store the leave days; move open tasks planned on full days off to `target_for(day)`.
    Returns the moved tasks as they were before (for Undo)."""
    with transaction(conn):
        full_days = sorted(e.day for e in entries if e.part == TimeOffPart.FULL)
        moves: list[tuple[int, date]] = []
        before: dict[int, tasks_repo.TaskState] = {}
        plan_from = min([today, *full_days])
        for day in full_days:
            for item in plan_repo.items_on(conn, day):
                task = tasks_repo.get(conn, item.task_id, include_deleted=True)
                if task.deleted_at is not None or task.status not in ACTIVE_STATUSES:
                    continue
                before.setdefault(task.id, tasks_repo.capture(conn, task.id, plan_from))
                moves.append((item.id, target_for(day)))
        for entry in entries:
            time_off_repo.put(conn, entry)
        for item_id, target in moves:
            plan_repo.move_items(conn, [item_id], target)
        return list(before.values())


def undo(
    conn: Connection,
    previous: dict[date, TimeOff | None],
    tasks: Sequence[tuple[tasks_repo.TaskState, tasks_repo.TaskState]],
) -> None:
    """Put the days' leave back as it was and the moved tasks' plans with it."""
    with transaction(conn):
        tasks_repo.restore_many(conn, tasks)
        for day, entry in previous.items():
            time_off_repo.remove(conn, day)
            if entry is not None:
                time_off_repo.put(conn, entry)
