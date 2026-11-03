"""Persistence for projects and areas (D-043): General (id 1) is never deleted or archived."""

import sqlite3
from datetime import datetime
from gettext import gettext as _

from daybook.core import timeutil, validation
from daybook.core.db import Connection, transaction
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import Project, ProjectColor, ProjectDraft
from daybook.core.repo import tasks as tasks_repo

_COLUMNS = "id, name, color, key_prefix, url_template, archived, sort_order, created_at"


def _from_row(row: sqlite3.Row) -> Project:
    try:
        color = ProjectColor(row["color"])
    except ValueError:
        color = ProjectColor.SLATE
    return Project(
        id=row["id"],
        name=row["name"],
        color=color,
        key_prefix=row["key_prefix"],
        url_template=row["url_template"],
        archived=bool(row["archived"]),
        sort_order=row["sort_order"],
        created_at=timeutil.from_db(row["created_at"]),
    )


def list_all(conn: Connection, *, include_archived: bool = False) -> list[Project]:
    """Sidebar order: by sort order, then name; General sorts last (D-051)."""
    where = "" if include_archived else "WHERE archived = 0"
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM project {where} ORDER BY sort_order, name COLLATE NOCASE, id"
    )
    return [_from_row(row) for row in rows]


def get(conn: Connection, project_id: int) -> Project:
    row = conn.execute(f"SELECT {_COLUMNS} FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError("project", project_id)
    return _from_row(row)


def _check_unique(conn: Connection, draft: ProjectDraft, exclude_id: int | None) -> None:
    other = exclude_id if exclude_id is not None else -1
    if conn.execute(
        "SELECT 1 FROM project WHERE name = ? COLLATE NOCASE AND id <> ?", (draft.name, other)
    ).fetchone():
        raise ValidationError("name", _("A project or area with this name already exists."))
    if (
        draft.key_prefix
        and conn.execute(
            "SELECT 1 FROM project WHERE key_prefix = ? COLLATE NOCASE AND id <> ?",
            (draft.key_prefix, other),
        ).fetchone()
    ):
        raise ValidationError("key_prefix", _("Another project already uses this prefix."))


def create(conn: Connection, draft: ProjectDraft, now: datetime) -> int:
    clean = validation.project_draft(draft)
    with transaction(conn):
        _check_unique(conn, clean, None)
        cursor = conn.execute(
            "INSERT INTO project (name, color, key_prefix, url_template, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                clean.name,
                clean.color.value,
                clean.key_prefix,
                clean.url_template,
                timeutil.to_db(now),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def update(conn: Connection, project_id: int, draft: ProjectDraft) -> None:
    clean = validation.project_draft(draft)
    with transaction(conn):
        get(conn, project_id)
        _check_unique(conn, clean, project_id)
        conn.execute(
            "UPDATE project SET name = ?, color = ?, key_prefix = ?, url_template = ? WHERE id = ?",
            (clean.name, clean.color.value, clean.key_prefix, clean.url_template, project_id),
        )


def set_archived(conn: Connection, project_id: int, archived: bool) -> None:
    with transaction(conn):
        project = get(conn, project_id)
        if project.is_general and archived:
            raise ValidationError("project", _("General can't be archived."))
        conn.execute("UPDATE project SET archived = ? WHERE id = ?", (int(archived), project_id))


def open_task_count(conn: Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM task WHERE project_id = ? AND deleted_at IS NULL "
        "AND status <> 'done'",
        (project_id,),
    ).fetchone()
    return int(row[0])


def live_task_count(conn: Connection, project_id: int) -> int:
    """Tasks that keep a project from being deleted: every task not in the trash."""
    row = conn.execute(
        "SELECT count(*) FROM task WHERE project_id = ? AND deleted_at IS NULL", (project_id,)
    ).fetchone()
    return int(row[0])


def delete(conn: Connection, project_id: int) -> None:
    """Delete a project that has no tasks (D-018); one with tasks can only be archived.

    Tasks already deleted (waiting to be purged, D-017) are purged with it.
    """
    with transaction(conn):
        project = get(conn, project_id)
        if project.is_general:
            raise ValidationError("project", _("General can't be deleted."))
        if live_task_count(conn, project_id):
            raise ValidationError(
                "project", _("This project has tasks. Archive it instead, to keep them.")
            )
        conn.execute("DELETE FROM task WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM project WHERE id = ?", (project_id,))
        tasks_repo.drop_unused_tags(conn)
