"""Day plan items: the parts Phase 1 needs (plan a task, move it, take it out of the plan).

A plan item belongs to one local date. Items on past dates are history and are never
changed by these functions (SPEC A5: "the original day keeps its history").
"""

import sqlite3
from datetime import date

from daybook.core import timeutil
from daybook.core.carryover import CarriedItem
from daybook.core.db import Connection, transaction
from daybook.core.errors import ChangedSinceError, NotFoundError
from daybook.core.models import PlanItem
from daybook.core.repo import app_state

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


def get(conn: Connection, item_id: int) -> PlanItem:
    row = conn.execute(f"SELECT {_COLUMNS} FROM day_plan_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFoundError("plan item", item_id)
    return _from_row(row)


def set_allotment(conn: Connection, item_id: int, allotted_min: int | None) -> PlanItem:
    with transaction(conn):
        get(conn, item_id)
        conn.execute(
            "UPDATE day_plan_item SET allotted_min = ? WHERE id = ?", (allotted_min, item_id)
        )
        return get(conn, item_id)


def remove_item(conn: Connection, item_id: int) -> PlanItem:
    with transaction(conn):
        item = get(conn, item_id)
        conn.execute("DELETE FROM day_plan_item WHERE id = ?", (item_id,))
        return item


def reinsert(conn: Connection, item: PlanItem) -> int:
    """Put a removed item back at its position on its day (Undo). Raises NotFoundError if
    the task is gone and ChangedSinceError if it was planned on that day again."""
    with transaction(conn):
        if conn.execute("SELECT 1 FROM task WHERE id = ?", (item.task_id,)).fetchone() is None:
            raise NotFoundError("task", item.task_id)
        if conn.execute(
            "SELECT 1 FROM day_plan_item WHERE plan_date = ? AND task_id = ?",
            (timeutil.date_to_db(item.plan_date), item.task_id),
        ).fetchone():
            raise ChangedSinceError(f"task {item.task_id} was planned again")
        cursor = conn.execute(
            "INSERT INTO day_plan_item (plan_date, task_id, allotted_min, position, "
            "carry_count, carried_from) VALUES (?, ?, ?, ?, ?, ?)",
            (
                timeutil.date_to_db(item.plan_date),
                item.task_id,
                item.allotted_min,
                item.position,
                item.carry_count,
                timeutil.date_to_db(item.carried_from) if item.carried_from else None,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def reorder(conn: Connection, day: date, ordered_ids: list[int]) -> None:
    """Number the day's items in the given order; items not listed keep their order after."""
    with transaction(conn):
        current = [item.id for item in items_on(conn, day)]
        listed = [item_id for item_id in ordered_ids if item_id in current]
        rest = [item_id for item_id in current if item_id not in listed]
        conn.executemany(
            "UPDATE day_plan_item SET position = ? WHERE id = ?",
            list(enumerate([*listed, *rest])),
        )


def since(conn: Connection, after: date | None, before: date) -> list[sqlite3.Row]:
    """Plan items dated on or after `after` (all, if None) and before `before`, with each
    task's status and whether it is deleted: the carry-over sources (PLAN §5.2).

    `after` is the day of the last rollover. That day's plan is still unfinished business
    (it is the previous working day's plan, and includes what the last run carried onto
    it), so it is included: excluding it made carry-over work for one day only."""
    clauses = ["d.plan_date < ?"]
    params: list[str] = [timeutil.date_to_db(before)]
    if after is not None:
        clauses.append("d.plan_date >= ?")
        params.append(timeutil.date_to_db(after))
    return list(
        conn.execute(
            "SELECT d.plan_date, d.position, d.task_id, d.allotted_min, d.carry_count, "
            "t.status, t.deleted_at IS NOT NULL AS deleted FROM day_plan_item d "
            f"JOIN task t ON t.id = d.task_id WHERE {' AND '.join(clauses)}",
            params,
        )
    )


def add_carried(conn: Connection, day: date, item: CarriedItem) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO day_plan_item (plan_date, task_id, allotted_min, position, "
            "carry_count, carried_from) VALUES (?, ?, ?, (SELECT coalesce(max(position), -1) "
            "+ 1 FROM day_plan_item WHERE plan_date = ?), ?, ?)",
            (
                timeutil.date_to_db(day),
                item.task_id,
                item.allotted_min,
                timeutil.date_to_db(day),
                item.carry_count,
                timeutil.date_to_db(item.carried_from),
            ),
        )


def move_items(conn: Connection, item_ids: list[int], target: date) -> None:
    """Move plan items to `target`, appended in order, each counting as a move (D-047).
    A task already planned on `target` keeps that item; the moved one is dropped."""
    stored = timeutil.date_to_db(target)
    with transaction(conn):
        for item_id in item_ids:
            item = get(conn, item_id)
            if item.plan_date == target:
                continue
            taken = conn.execute(
                "SELECT 1 FROM day_plan_item WHERE plan_date = ? AND task_id = ?",
                (stored, item.task_id),
            ).fetchone()
            if taken:
                conn.execute("DELETE FROM day_plan_item WHERE id = ?", (item_id,))
            else:
                conn.execute(
                    "UPDATE day_plan_item SET plan_date = ?, carry_count = 0, "
                    "carried_from = NULL, position = (SELECT coalesce(max(position), -1) + 1 "
                    "FROM day_plan_item WHERE plan_date = ?) WHERE id = ?",
                    (stored, stored, item_id),
                )
            conn.execute(
                "UPDATE task SET moved_count = moved_count + 1 WHERE id = ?", (item.task_id,)
            )


def apply_rollover(
    conn: Connection, day: date, items: list[CarriedItem], watermark_key: str
) -> None:
    """Append carried items to `day` and advance the rollover watermark, atomically, so
    running the rollover twice changes nothing (PLAN §5.2)."""
    with transaction(conn):
        for item in items:
            add_carried(conn, day, item)
        app_state.put(conn, watermark_key, timeutil.date_to_db(day))
