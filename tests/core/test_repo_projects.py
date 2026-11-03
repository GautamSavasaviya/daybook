import sqlite3
from datetime import UTC, datetime

import pytest

from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import GENERAL_PROJECT_ID, ProjectColor, ProjectDraft, TaskDraft
from daybook.core.repo import projects, tasks

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def test_general_is_seeded_and_sorts_last(conn):
    projects.create(conn, ProjectDraft("Web", ProjectColor.TEAL, "web"), NOW)
    projects.create(conn, ProjectDraft("docs"), NOW)
    names = [p.name for p in projects.list_all(conn)]
    assert names == ["docs", "Web", "General"]
    general = projects.get(conn, GENERAL_PROJECT_ID)
    assert general.is_general
    assert general.is_area
    assert general.color is ProjectColor.SLATE


def test_create_normalises_and_reads_back(conn):
    pid = projects.create(
        conn, ProjectDraft(" PMS ", ProjectColor.RED, "proj", "https://pms/{key}"), NOW
    )
    project = projects.get(conn, pid)
    assert (project.name, project.key_prefix, project.url_template) == (
        "PMS",
        "PROJ",
        "https://pms/{key}",
    )
    assert not project.is_area
    assert project.created_at == NOW


def test_names_and_prefixes_are_unique_case_insensitively(conn):
    pid = projects.create(conn, ProjectDraft("Web", key_prefix="WEB"), NOW)
    with pytest.raises(ValidationError) as info:
        projects.create(conn, ProjectDraft("web"), NOW)
    assert info.value.field == "name"
    with pytest.raises(ValidationError) as info:
        projects.create(conn, ProjectDraft("Other", key_prefix="web"), NOW)
    assert info.value.field == "key_prefix"
    projects.update(conn, pid, ProjectDraft("WEB site", ProjectColor.GREEN, "web"))
    assert projects.get(conn, pid).name == "WEB site"


def test_update_and_missing_projects(conn):
    with pytest.raises(NotFoundError):
        projects.get(conn, 99)
    with pytest.raises(NotFoundError):
        projects.update(conn, 99, ProjectDraft("x"))
    projects.update(conn, GENERAL_PROJECT_ID, ProjectDraft("Misc", ProjectColor.PINK))
    assert projects.get(conn, GENERAL_PROJECT_ID).name == "Misc"


def test_archive_hides_from_default_list_but_general_cannot_be_archived(conn):
    pid = projects.create(conn, ProjectDraft("Old"), NOW)
    projects.set_archived(conn, pid, True)
    assert pid not in [p.id for p in projects.list_all(conn)]
    assert projects.get(conn, pid).archived
    assert pid in [p.id for p in projects.list_all(conn, include_archived=True)]
    projects.set_archived(conn, pid, False)
    assert not projects.get(conn, pid).archived
    with pytest.raises(ValidationError):
        projects.set_archived(conn, GENERAL_PROJECT_ID, True)
    # The database trigger is the backstop.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE project SET archived = 1 WHERE id = 1")


def test_delete_only_without_live_tasks_and_purges_trashed_ones(conn):
    pid = projects.create(conn, ProjectDraft("Temp"), NOW)
    task_id = tasks.create(conn, TaskDraft("t", project_id=pid), NOW)
    assert projects.open_task_count(conn, pid) == 1
    with pytest.raises(ValidationError):
        projects.delete(conn, pid)
    tasks.complete(conn, task_id, NOW)
    assert projects.open_task_count(conn, pid) == 0
    assert projects.live_task_count(conn, pid) == 1
    tasks.soft_delete(conn, task_id, NOW)
    assert projects.live_task_count(conn, pid) == 0
    projects.delete(conn, pid)
    with pytest.raises(NotFoundError):
        projects.get(conn, pid)
    with pytest.raises(NotFoundError):
        tasks.get(conn, task_id, include_deleted=True)
    with pytest.raises(ValidationError):
        projects.delete(conn, GENERAL_PROJECT_ID)


def test_unknown_stored_colour_falls_back_to_slate(conn):
    conn.execute("UPDATE project SET color = 'mauve' WHERE id = 1")
    assert projects.get(conn, 1).color is ProjectColor.SLATE
