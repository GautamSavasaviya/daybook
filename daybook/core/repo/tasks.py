"""Persistence for tasks, their tags and checklists, and the task lists (D-051).

Deleting is soft (D-017): `deleted_at` is set, the row is hidden everywhere, and rows
deleted more than a day ago are purged at startup.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from gettext import gettext as _
from typing import Any

from daybook.core import recurrence as recurrence_rules
from daybook.core import reschedule, timeutil, validation
from daybook.core.db import Connection, transaction
from daybook.core.errors import ChangedSinceError, NotFoundError, ValidationError
from daybook.core.models import (
    ChecklistItem,
    Hold,
    HoldReason,
    PlanItem,
    PlanSlot,
    Priority,
    ProjectColor,
    Recurrence,
    SidebarCounts,
    Task,
    TaskDraft,
    TaskStatus,
    TaskSummary,
)
from daybook.core.repo import plan as plan_repo
from daybook.core.tasklists import ListKind, TaskFilter, TaskQuery

_TASK_COLUMNS = (
    "t.id, t.project_id, t.title, t.ticket_key, t.requested_by, t.priority, t.status, "
    "t.hold_reason, t.hold_note, t.hold_since, t.follow_up_date, t.estimate_min, t.due_date, "
    "t.notes, t.moved_count, t.created_at, t.updated_at, t.completed_at, t.deleted_at, "
    "t.recurrence, t.recurs_from_id"
)
_TAG_SEPARATOR = "\x1f"

log = logging.getLogger(__name__)


def _date_or_none(text: str | None) -> date | None:
    return timeutil.date_from_db(text) if text else None


def _instant_or_none(text: str | None) -> datetime | None:
    return timeutil.from_db(text) if text else None


def _recurrence(text: str | None, task_id: int) -> Recurrence | None:
    try:
        return recurrence_rules.from_json(text)
    except (ValueError, KeyError, ValidationError) as exc:
        # A damaged rule must not make the task unreadable; it just stops repeating.
        log.warning("ignoring the recurrence of task %d (%r): %s", task_id, text, exc)
        return None


def _task_from_row(row: sqlite3.Row) -> Task:
    hold = None
    if row["hold_reason"] is not None:
        hold = Hold(
            reason=HoldReason(row["hold_reason"]),
            note=row["hold_note"],
            since=timeutil.from_db(row["hold_since"]),
            follow_up=_date_or_none(row["follow_up_date"]),
        )
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        ticket_key=row["ticket_key"],
        requested_by=row["requested_by"],
        priority=Priority(row["priority"]),
        status=TaskStatus(row["status"]),
        hold=hold,
        estimate_min=row["estimate_min"],
        due_date=_date_or_none(row["due_date"]),
        notes=row["notes"],
        moved_count=row["moved_count"],
        created_at=timeutil.from_db(row["created_at"]),
        updated_at=timeutil.from_db(row["updated_at"]),
        completed_at=_instant_or_none(row["completed_at"]),
        deleted_at=_instant_or_none(row["deleted_at"]),
        recurrence=_recurrence(row["recurrence"], row["id"]),
        recurs_from_id=row["recurs_from_id"],
    )


def _recurrence_json(rule: Recurrence | None) -> str | None:
    return recurrence_rules.to_json(recurrence_rules.validate(rule)) if rule else None


# --- Single tasks ---------------------------------------------------------------------


def get(conn: Connection, task_id: int, *, include_deleted: bool = False) -> Task:
    row = conn.execute(f"SELECT {_TASK_COLUMNS} FROM task t WHERE t.id = ?", (task_id,)).fetchone()
    if row is None or (row["deleted_at"] is not None and not include_deleted):
        raise NotFoundError("task", task_id)
    return _task_from_row(row)


def _check_project(conn: Connection, project_id: int, *, allow_archived: bool = False) -> None:
    row = conn.execute("SELECT archived FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None or (row["archived"] and not allow_archived):
        raise ValidationError("project", _("Choose an active project or area."))


def create(conn: Connection, draft: TaskDraft, now: datetime) -> int:
    clean = validation.task_draft(draft)
    stamp = timeutil.to_db(now)
    with transaction(conn):
        _check_project(conn, clean.project_id)
        cursor = conn.execute(
            "INSERT INTO task (project_id, title, ticket_key, requested_by, priority, "
            "estimate_min, due_date, notes, recurrence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clean.project_id,
                clean.title,
                clean.ticket_key,
                clean.requested_by,
                int(clean.priority),
                clean.estimate_min,
                timeutil.date_to_db(clean.due_date) if clean.due_date else None,
                clean.notes,
                _recurrence_json(clean.recurrence),
                stamp,
                stamp,
            ),
        )
        task_id = cursor.lastrowid
        assert task_id is not None
        set_tags(conn, task_id, clean.tags)
        if clean.plan is not None:
            plan_repo.add(
                conn, task_id, clean.plan.day, validation.allotment(clean.plan.allotted_min)
            )
        return task_id


def update(conn: Connection, task: Task, now: datetime) -> Task:
    """Save the editable fields of `task` (title, project, ticket, requested by, priority,
    estimate, due date, notes). Status changes have their own functions."""
    title = validation.title(task.title)
    ticket = validation.ticket_key(task.ticket_key)
    requested_by = validation.optional_text(
        task.requested_by, "requested_by", validation.MAX_REQUESTED_BY
    )
    priority = validation.priority(task.priority)
    estimate = validation.estimate(task.estimate_min)
    notes = validation.notes(task.notes)
    with transaction(conn):
        current = get(conn, task.id)
        # A task may stay in its (now archived) project, but can't be moved into one.
        _check_project(conn, task.project_id, allow_archived=task.project_id == current.project_id)
        conn.execute(
            "UPDATE task SET project_id = ?, title = ?, ticket_key = ?, requested_by = ?, "
            "priority = ?, estimate_min = ?, due_date = ?, notes = ?, recurrence = ?, "
            "updated_at = ? WHERE id = ?",
            (
                task.project_id,
                title,
                ticket,
                requested_by,
                int(priority),
                estimate,
                timeutil.date_to_db(task.due_date) if task.due_date else None,
                notes,
                _recurrence_json(task.recurrence),
                timeutil.to_db(now),
                task.id,
            ),
        )
        return get(conn, task.id)


def set_status(conn: Connection, task_id: int, status: TaskStatus, now: datetime) -> None:
    """To do or In progress. Leaving On hold or Done clears the hold and completion."""
    if status not in (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS):
        raise ValueError(f"use complete() or put_on_hold() for {status}")
    with transaction(conn):
        get(conn, task_id)
        conn.execute(
            "UPDATE task SET status = ?, hold_reason = NULL, hold_note = NULL, "
            "hold_since = NULL, follow_up_date = NULL, completed_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (status.value, timeutil.to_db(now), task_id),
        )


def complete(conn: Connection, task_id: int, now: datetime, today: date) -> int | None:
    """Mark the task done. A repeating task gets its next instance in the same
    transaction (D-016); its id is returned (None otherwise)."""
    stamp = timeutil.to_db(now)
    with transaction(conn):
        task = get(conn, task_id)
        if task.status == TaskStatus.DONE:
            return None
        conn.execute(
            "UPDATE task SET status = 'done', completed_at = ?, hold_reason = NULL, "
            "hold_note = NULL, hold_since = NULL, follow_up_date = NULL, updated_at = ? "
            "WHERE id = ?",
            (stamp, stamp, task_id),
        )
        if task.recurrence is None:
            return None
        return _next_instance(conn, task, today, stamp)


def _next_instance(conn: Connection, task: Task, today: date, stamp: str) -> int | None:
    """Copy a completed repeating task to its next due date, checklist unticked (D-016)."""
    assert task.recurrence is not None
    anchor = task.due_date or today
    due = recurrence_rules.next_date(task.recurrence, anchor, task.due_date or today)
    if due is None:
        return None  # the rule ended ("until" has passed)
    cursor = conn.execute(
        "INSERT INTO task (project_id, title, ticket_key, requested_by, priority, "
        "estimate_min, due_date, notes, recurrence, recurs_from_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task.project_id,
            task.title,
            task.ticket_key,
            task.requested_by,
            int(task.priority),
            task.estimate_min,
            timeutil.date_to_db(due),
            task.notes,
            _recurrence_json(task.recurrence),
            task.id,
            stamp,
            stamp,
        ),
    )
    new_id = cursor.lastrowid
    assert new_id is not None
    conn.execute(
        "INSERT INTO task_tag (task_id, tag_id) SELECT ?, tag_id FROM task_tag WHERE task_id = ?",
        (new_id, task.id),
    )
    conn.execute(
        "INSERT INTO checklist_item (task_id, text, done, position) "
        "SELECT ?, text, 0, position FROM checklist_item WHERE task_id = ?",
        (new_id, task.id),
    )
    return new_id


def put_on_hold(
    conn: Connection, task_id: int, hold: Hold, today: date, now: datetime
) -> list[PlanItem]:
    """Hold the task and take it out of today's and later plans (D-046).

    Returns the plan items removed. Past plan items stay as history.
    """
    with transaction(conn):
        get(conn, task_id)
        conn.execute(
            "UPDATE task SET status = 'on_hold', hold_reason = ?, hold_note = ?, "
            "hold_since = ?, follow_up_date = ?, completed_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (
                hold.reason.value,
                hold.note,
                timeutil.to_db(hold.since),
                timeutil.date_to_db(hold.follow_up) if hold.follow_up else None,
                timeutil.to_db(now),
                task_id,
            ),
        )
        return plan_repo.remove_from(conn, task_id, today)


def soft_delete(conn: Connection, task_id: int, now: datetime) -> None:
    with transaction(conn):
        get(conn, task_id)
        conn.execute("UPDATE task SET deleted_at = ? WHERE id = ?", (timeutil.to_db(now), task_id))


def undelete(conn: Connection, task_id: int) -> None:
    with transaction(conn):
        get(conn, task_id, include_deleted=True)
        conn.execute("UPDATE task SET deleted_at = NULL WHERE id = ?", (task_id,))


def purge_deleted(conn: Connection, deleted_before: datetime) -> int:
    """Really delete tasks soft-deleted before the cut-off, with their checklist, tags, plan
    items and time entries (cascades). Returns how many tasks were purged."""
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM task WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (timeutil.to_db(deleted_before),),
        )
        drop_unused_tags(conn)
        return cursor.rowcount


def move(
    conn: Connection,
    task_id: int,
    today: date,
    target: date | None,
    *,
    note: str | None,
    now: datetime,
) -> bool:
    """Move the task's plan (today and later) to `target`, or out of the plan (None).

    A planned task keeps its allotment. An unplanned one gets the default allotment (D-021).
    Returns True when a planned item really moved, which counts towards "moved ×N" (D-047).
    """
    with transaction(conn):
        task = get(conn, task_id)
        items = plan_repo.items_for_task(conn, task_id, today)
        unchanged = len(items) == 1 and items[0].plan_date == target
        moved = bool(items) and not unchanged
        if not unchanged:
            plan_repo.remove_from(conn, task_id, today)
            if target is not None:
                allotted = (
                    items[0].allotted_min
                    if items
                    else reschedule.default_allotment(task.estimate_min)
                )
                plan_repo.add(conn, task_id, target, allotted)
        conn.execute(
            "UPDATE task SET moved_count = moved_count + ?, notes = ?, updated_at = ? WHERE id = ?",
            (int(moved), reschedule.append_note(task.notes, note), timeutil.to_db(now), task_id),
        )
        return moved


# --- Undo snapshots (D-039) ------------------------------------------------------------


@dataclass(frozen=True)
class TaskState:
    """A task row and its plan from `plan_from` on, as captured before a change."""

    task: Task
    plan_from: date
    plan_items: tuple[PlanItem, ...]


def capture(conn: Connection, task_id: int, plan_from: date) -> TaskState:
    return TaskState(
        task=get(conn, task_id, include_deleted=True),
        plan_from=plan_from,
        plan_items=tuple(plan_repo.items_for_task(conn, task_id, plan_from)),
    )


def restore(
    conn: Connection,
    state: TaskState,
    expected: TaskState | None = None,
    *,
    remove: TaskState | None = None,
) -> None:
    """Put the task back exactly as captured in `state`.

    With `expected` (the state right after the action being undone), nothing is written
    if the task or its plan changed since: Undo must never overwrite a later edit. Raises
    ChangedSinceError then, and NotFoundError if the task was purged.
    """
    task = state.task
    hold = task.hold
    with transaction(conn):
        get(conn, task.id, include_deleted=True)
        if expected is not None and capture(conn, task.id, state.plan_from) != expected:
            raise ChangedSinceError(f"task {task.id} changed since the action")
        if remove is not None:
            # The next instance a completion created goes away with the Undo (D-016),
            # unless the user has worked on it since.
            if capture(conn, remove.task.id, remove.plan_from) != remove:
                raise ChangedSinceError(f"task {remove.task.id} changed since the action")
            conn.execute("DELETE FROM task WHERE id = ?", (remove.task.id,))
        conn.execute(
            "UPDATE task SET project_id = ?, title = ?, ticket_key = ?, requested_by = ?, "
            "priority = ?, status = ?, hold_reason = ?, hold_note = ?, hold_since = ?, "
            "follow_up_date = ?, estimate_min = ?, due_date = ?, notes = ?, moved_count = ?, "
            "recurrence = ?, updated_at = ?, completed_at = ?, deleted_at = ? WHERE id = ?",
            (
                task.project_id,
                task.title,
                task.ticket_key,
                task.requested_by,
                int(task.priority),
                task.status.value,
                hold.reason.value if hold else None,
                hold.note if hold else None,
                timeutil.to_db(hold.since) if hold else None,
                timeutil.date_to_db(hold.follow_up) if hold and hold.follow_up else None,
                task.estimate_min,
                timeutil.date_to_db(task.due_date) if task.due_date else None,
                task.notes,
                task.moved_count,
                _recurrence_json(task.recurrence),
                timeutil.to_db(task.updated_at),
                timeutil.to_db(task.completed_at) if task.completed_at else None,
                timeutil.to_db(task.deleted_at) if task.deleted_at else None,
                task.id,
            ),
        )
        plan_repo.restore(conn, task.id, state.plan_from, list(state.plan_items))


# --- Tags ------------------------------------------------------------------------------


def tags_of(conn: Connection, task_id: int) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT g.name FROM task_tag tt JOIN tag g ON g.id = tt.tag_id WHERE tt.task_id = ? "
        "ORDER BY g.name COLLATE NOCASE",
        (task_id,),
    )
    return tuple(row["name"] for row in rows)


def set_tags(conn: Connection, task_id: int, names: Sequence[str]) -> None:
    clean = validation.tag_names(names)
    with transaction(conn):
        conn.execute("DELETE FROM task_tag WHERE task_id = ?", (task_id,))
        for name in clean:
            conn.execute("INSERT INTO tag (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
            conn.execute(
                "INSERT INTO task_tag (task_id, tag_id) "
                "SELECT ?, id FROM tag WHERE name = ? COLLATE NOCASE",
                (task_id, name),
            )
        drop_unused_tags(conn)


def drop_unused_tags(conn: Connection) -> None:
    # Tags nobody uses any more would only clutter the filter and suggestions.
    with transaction(conn):
        conn.execute("DELETE FROM tag WHERE id NOT IN (SELECT tag_id FROM task_tag)")


def all_tags(conn: Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT g.name FROM tag g JOIN task_tag tt ON tt.tag_id = g.id "
        "JOIN task t ON t.id = tt.task_id WHERE t.deleted_at IS NULL "
        "ORDER BY g.name COLLATE NOCASE"
    )
    return [row["name"] for row in rows]


# --- Checklist -------------------------------------------------------------------------


def _checklist_from_row(row: sqlite3.Row) -> ChecklistItem:
    return ChecklistItem(
        id=row["id"],
        task_id=row["task_id"],
        text=row["text"],
        done=bool(row["done"]),
        position=row["position"],
    )


def checklist(conn: Connection, task_id: int) -> list[ChecklistItem]:
    rows = conn.execute(
        "SELECT id, task_id, text, done, position FROM checklist_item WHERE task_id = ? "
        "ORDER BY position, id",
        (task_id,),
    )
    return [_checklist_from_row(row) for row in rows]


def _checklist_item(conn: Connection, item_id: int) -> ChecklistItem:
    row = conn.execute(
        "SELECT id, task_id, text, done, position FROM checklist_item WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("checklist item", item_id)
    return _checklist_from_row(row)


def add_checklist_item(conn: Connection, task_id: int, text: str) -> int:
    clean = validation.checklist_text(text)
    with transaction(conn):
        get(conn, task_id)
        cursor = conn.execute(
            "INSERT INTO checklist_item (task_id, text, position) VALUES (?, ?, "
            "(SELECT coalesce(max(position), -1) + 1 FROM checklist_item WHERE task_id = ?))",
            (task_id, clean, task_id),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def set_checklist_done(conn: Connection, item_id: int, done: bool) -> ChecklistItem:
    with transaction(conn):
        _checklist_item(conn, item_id)
        conn.execute("UPDATE checklist_item SET done = ? WHERE id = ?", (int(done), item_id))
        return _checklist_item(conn, item_id)


def rename_checklist_item(conn: Connection, item_id: int, text: str) -> ChecklistItem:
    clean = validation.checklist_text(text)
    with transaction(conn):
        _checklist_item(conn, item_id)
        conn.execute("UPDATE checklist_item SET text = ? WHERE id = ?", (clean, item_id))
        return _checklist_item(conn, item_id)


def delete_checklist_item(conn: Connection, item_id: int) -> ChecklistItem:
    with transaction(conn):
        item = _checklist_item(conn, item_id)
        conn.execute("DELETE FROM checklist_item WHERE id = ?", (item_id,))
        return item


def restore_checklist_item(conn: Connection, item: ChecklistItem) -> None:
    with transaction(conn):
        get(conn, item.task_id, include_deleted=True)
        # A new id: the old one may have been reused since (no AUTOINCREMENT).
        conn.execute(
            "INSERT INTO checklist_item (task_id, text, done, position) VALUES (?, ?, ?, ?)",
            (item.task_id, item.text, int(item.done), item.position),
        )


# --- Lookups for forms -----------------------------------------------------------------


def requested_by_values(conn: Connection, limit: int = 200) -> list[str]:
    """Names used before, most used first (autocomplete, D-044)."""
    rows = conn.execute(
        "SELECT requested_by AS name FROM task WHERE requested_by IS NOT NULL "
        "AND deleted_at IS NULL GROUP BY requested_by COLLATE NOCASE "
        "ORDER BY count(*) DESC, max(updated_at) DESC LIMIT ?",
        (limit,),
    )
    return [row["name"] for row in rows]


def ticket_key_in_use(conn: Connection, key: str, exclude_id: int | None = None) -> bool:
    """Ticket keys may repeat (D-018); the forms warn when one does."""
    row = conn.execute(
        "SELECT 1 FROM task WHERE ticket_key = ? COLLATE NOCASE AND deleted_at IS NULL "
        "AND id <> ? LIMIT 1",
        (key, exclude_id if exclude_id is not None else -1),
    ).fetchone()
    return row is not None


# --- Lists -----------------------------------------------------------------------------

_ACTIVE = "t.status IN ('todo', 'in_progress')"
_VISIBLE = "t.deleted_at IS NULL AND p.archived = 0"
_PLANNED_FROM_TODAY = (
    "EXISTS (SELECT 1 FROM day_plan_item d WHERE d.task_id = t.id AND d.plan_date >= :today)"
)
_PLANNED_IN_WINDOW = (
    "EXISTS (SELECT 1 FROM day_plan_item d WHERE d.task_id = t.id "
    "AND d.plan_date >= :today AND d.plan_date < :horizon)"
)
_INBOX = f"{_VISIBLE} AND {_ACTIVE} AND NOT {_PLANNED_FROM_TODAY}"
_UPCOMING_OPEN = f"{_VISIBLE} AND {_ACTIVE} AND (t.due_date < :horizon OR {_PLANNED_IN_WINDOW})"
_UPCOMING = f"({_UPCOMING_OPEN}) OR ({_VISIBLE} AND t.status = 'on_hold')"
# "Plan my day": overdue or due by the planned day, follow-ups due, P1/P2 Inbox tasks;
# never a task already planned for that day (PLAN §5.1).
_CANDIDATES = (
    f"{_VISIBLE} AND NOT EXISTS (SELECT 1 FROM day_plan_item d WHERE d.task_id = t.id "
    "AND d.plan_date = :plan_day) AND ("
    f"({_ACTIVE} AND (t.due_date <= :plan_day OR (t.priority <= 2 AND NOT {_PLANNED_FROM_TODAY})))"
    " OR (t.status = 'on_hold' AND t.follow_up_date <= :plan_day))"
)

_SUMMARY_SELECT = f"""
SELECT {_TASK_COLUMNS},
  p.name AS project_name, p.color AS project_color, p.archived AS project_archived,
  (SELECT group_concat(g.name, char(31)) FROM task_tag tt JOIN tag g ON g.id = tt.tag_id
    WHERE tt.task_id = t.id) AS tag_names,
  np.plan_date AS np_date, np.allotted_min AS np_allotted, np.carry_count AS np_carry,
  (SELECT count(*) FROM checklist_item c WHERE c.task_id = t.id) AS cl_total,
  (SELECT count(*) FROM checklist_item c WHERE c.task_id = t.id AND c.done = 1) AS cl_done
FROM task t
JOIN project p ON p.id = t.project_id
LEFT JOIN day_plan_item np ON np.id = (
  SELECT d.id FROM day_plan_item d WHERE d.task_id = t.id AND d.plan_date >= :today
  ORDER BY d.plan_date LIMIT 1)
"""

_ORDER_OPEN = "t.priority, t.due_date IS NULL, t.due_date, t.created_at, t.id"


def _summary_from_row(row: sqlite3.Row) -> TaskSummary:
    try:
        color = ProjectColor(row["project_color"])
    except ValueError:
        color = ProjectColor.SLATE
    tag_names = row["tag_names"]
    next_plan = None
    if row["np_date"] is not None:
        next_plan = PlanSlot(
            day=timeutil.date_from_db(row["np_date"]),
            allotted_min=row["np_allotted"],
            carry_count=row["np_carry"],
        )
    return TaskSummary(
        task=_task_from_row(row),
        project_name=row["project_name"],
        project_color=color,
        project_archived=bool(row["project_archived"]),
        tags=tuple(sorted(tag_names.split(_TAG_SEPARATOR), key=str.casefold)) if tag_names else (),
        next_plan=next_plan,
        checklist_done=row["cl_done"],
        checklist_total=row["cl_total"],
    )


def _filter_sql(task_filter: TaskFilter, params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    if task_filter.project_id is not None:
        clauses.append("t.project_id = :f_project")
        params["f_project"] = task_filter.project_id
    if task_filter.status is not None:
        clauses.append("t.status = :f_status")
        params["f_status"] = task_filter.status.value
    if task_filter.priority is not None:
        clauses.append("t.priority = :f_priority")
        params["f_priority"] = int(task_filter.priority)
    if task_filter.tag is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM task_tag tt JOIN tag g ON g.id = tt.tag_id "
            "WHERE tt.task_id = t.id AND g.name = :f_tag COLLATE NOCASE)"
        )
        params["f_tag"] = task_filter.tag
    if task_filter.requested_by is not None:
        clauses.append("t.requested_by = :f_requested_by COLLATE NOCASE")
        params["f_requested_by"] = task_filter.requested_by
    return clauses


def list_tasks(conn: Connection, query: TaskQuery) -> list[TaskSummary]:
    """The tasks of one list, sorted: priority, then due date (none last), then created
    (D-051); the Done list shows the most recently completed first."""
    params: dict[str, Any] = {
        "today": timeutil.date_to_db(query.today),
        "horizon": timeutil.date_to_db(query.horizon),
    }
    order = _ORDER_OPEN
    match query.kind:
        case ListKind.INBOX:
            clauses = [_INBOX]
        case ListKind.UPCOMING:
            clauses = [f"({_UPCOMING})"]
        case ListKind.PROJECT:
            if query.project_id is None:
                raise ValueError("a PROJECT list needs project_id")
            clauses = ["t.deleted_at IS NULL", "t.status <> 'done'", "t.project_id = :project"]
            params["project"] = query.project_id
        case ListKind.DONE:
            clauses = ["t.deleted_at IS NULL", "t.status = 'done'"]
            order = "t.completed_at DESC, t.id DESC"
        case ListKind.CANDIDATES:
            params["plan_day"] = timeutil.date_to_db(query.plan_day or query.today)
            clauses = [_CANDIDATES]
        case ListKind.SEARCH:
            words = query.text.casefold().split()
            if not words:
                return []
            clauses = ["t.deleted_at IS NULL"]
            for index, word in enumerate(words):
                key = f"w{index}"
                params[key] = word
                clauses.append(
                    f"(instr(fold(t.title), :{key}) > 0 "
                    f"OR instr(fold(coalesce(t.ticket_key, '')), :{key}) > 0 "
                    f"OR instr(fold(t.notes), :{key}) > 0)"
                )
            order = (
                "t.status = 'done', CASE WHEN t.status = 'done' THEN t.completed_at END DESC, "
                + _ORDER_OPEN
            )
    clauses += _filter_sql(query.filter, params)
    sql = f"{_SUMMARY_SELECT} WHERE {' AND '.join(clauses)} ORDER BY {order}"
    return [_summary_from_row(row) for row in conn.execute(sql, params)]


def summaries(conn: Connection, task_ids: Sequence[int], today: date) -> dict[int, TaskSummary]:
    """Row data for specific tasks (not in the trash), keyed by id."""
    if not task_ids:
        return {}
    params: dict[str, Any] = {"today": timeutil.date_to_db(today)}
    marks = []
    for index, task_id in enumerate(task_ids):
        params[f"id{index}"] = task_id
        marks.append(f":id{index}")
    sql = f"{_SUMMARY_SELECT} WHERE t.id IN ({', '.join(marks)}) AND t.deleted_at IS NULL"
    return {s.task.id: s for s in map(_summary_from_row, conn.execute(sql, params))}


def counts(conn: Connection, today: date, horizon: date) -> SidebarCounts:
    """Badge numbers for the sidebar; they match the lists' definitions exactly."""
    params = {"today": timeutil.date_to_db(today), "horizon": timeutil.date_to_db(horizon)}
    base = "SELECT count(*) FROM task t JOIN project p ON p.id = t.project_id WHERE "
    inbox = conn.execute(base + _INBOX, params).fetchone()[0]
    upcoming = conn.execute(base + _UPCOMING_OPEN, params).fetchone()[0]
    per_project = {
        int(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT project_id, count(*) FROM task WHERE deleted_at IS NULL "
            "AND status <> 'done' GROUP BY project_id"
        )
    }
    planned_today = conn.execute(
        "SELECT count(*) FROM day_plan_item d JOIN task t ON t.id = d.task_id "
        "WHERE d.plan_date = :today AND t.deleted_at IS NULL "
        "AND t.status IN ('todo', 'in_progress')",
        params,
    ).fetchone()[0]
    return SidebarCounts(
        inbox=int(inbox),
        upcoming=int(upcoming),
        per_project=per_project,
        today=int(planned_today),
    )


def restore_many(conn: Connection, changes: Sequence[tuple[TaskState, TaskState]]) -> None:
    """Undo a change to several tasks at once: each (before, after) pair is restored only
    if the task still looks like `after`; otherwise nothing at all is written."""
    with transaction(conn):
        for before, after in changes:
            restore(conn, before, expected=after)
