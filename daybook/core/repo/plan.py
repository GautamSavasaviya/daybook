"""Day plan items: the parts Phase 1 needs (plan a task, move it, take it out of the plan).

A plan item belongs to one local date. Items on past dates are history and are never
changed by these functions (SPEC A5: "the original day keeps its history").
"""

import sqlite3
from datetime import date

from daybook.core import timeutil
from daybook.core.db import Connection, transaction
from daybook.core.models import PlanItem

_COLUMNS = "id, plan_date, task_id, allotted_min, position, carry_count, carried_from"


def _from_row(row: sqlite3.Row) -> PlanItem:
    carried = row["carried_from"]
    return PlanItem(
        id=row["id"],
        plan_date=timeutil.date_from_db(row["plan_date"]),
        task_id=row["task_id"],
        allotted_min=row["allotted_min"],
        position=row["position"],
        carry_count=row["carry_count"],
        carried_from=timeutil.date_from_db(carried) if carried else None,
    )


def items_for_task(conn: Connection, task_id: int, from_day: date) -> list[PlanItem]:
    """The task's plan items on `from_day` or later, earliest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM day_plan_item WHERE task_id = ? AND plan_date >= ? "
        "ORDER BY plan_date",
        (task_id, timeutil.date_to_db(from_day)),
    )
    return [_from_row(row) for row in rows]


def items_on(conn: Connection, day: date) -> list[PlanItem]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM day_plan_item WHERE plan_date = ? ORDER BY position, id",
        (timeutil.date_to_db(day),),
    )
    return [_from_row(row) for row in rows]


def add(conn: Connection, task_id: int, day: date, allotted_min: int | None) -> int:
    """Append the task to `day`'s plan. If it is already planned that day, its allotment is
    replaced and its place kept."""
    stored_day = timeutil.date_to_db(day)
    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM day_plan_item WHERE plan_date = ? AND task_id = ?",
            (stored_day, task_id),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE day_plan_item SET allotted_min = ? WHERE id = ?",
                (allotted_min, existing["id"]),
            )
            return int(existing["id"])
        cursor = conn.execute(
            "INSERT INTO day_plan_item (plan_date, task_id, allotted_min, position) "
            "VALUES (?, ?, ?, (SELECT coalesce(max(position), -1) + 1 FROM day_plan_item "
            "WHERE plan_date = ?))",
            (stored_day, task_id, allotted_min, stored_day),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def remove_from(conn: Connection, task_id: int, from_day: date) -> list[PlanItem]:
    """Take the task out of every plan on `from_day` or later; returns what was removed."""
    with transaction(conn):
        removed = items_for_task(conn, task_id, from_day)
        conn.execute(
            "DELETE FROM day_plan_item WHERE task_id = ? AND plan_date >= ?",
            (task_id, timeutil.date_to_db(from_day)),
        )
        return removed


def restore(conn: Connection, task_id: int, from_day: date, items: list[PlanItem]) -> None:
    """Put back exactly `items` as the task's plan from `from_day` on (used by Undo)."""
    with transaction(conn):
        remove_from(conn, task_id, from_day)
        # New ids: the old ones may have been reused since (no AUTOINCREMENT).
        conn.executemany(
            "INSERT INTO day_plan_item (plan_date, task_id, allotted_min, position, "
            "carry_count, carried_from) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    timeutil.date_to_db(item.plan_date),
                    item.task_id,
                    item.allotted_min,
                    item.position,
                    item.carry_count,
                    timeutil.date_to_db(item.carried_from) if item.carried_from else None,
                )
                for item in items
            ],
        )
