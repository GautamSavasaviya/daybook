import sqlite3
from datetime import UTC, datetime

import pytest

from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import GENERAL_PROJECT_ID, ProjectColor, ProjectDraft, TaskDraft
from daybook.core.repo import projects, tasks, time_entries, timer

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = NOW.date()


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


def test_delete_takes_the_tasks_and_everything_on_them_with_it(conn):
    pid = projects.create(conn, ProjectDraft("Temp"), NOW)
    other = projects.create(conn, ProjectDraft("Keep"), NOW)
    task_id = tasks.create(conn, TaskDraft("t", project_id=pid, tags=("only-here",)), NOW)
    done = tasks.create(conn, TaskDraft("d", project_id=pid), NOW)
    kept = tasks.create(conn, TaskDraft("k", project_id=other, tags=("shared",)), NOW)
    trashed = tasks.create(conn, TaskDraft("x", project_id=pid), NOW)
    tasks.soft_delete(conn, trashed, NOW)
    tasks.complete(conn, done, NOW, TODAY)
    time_entries.add_manual(
        conn,
        task_id,
        datetime(2026, 11, 3, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 8, 45, tzinfo=UTC),
        note="",
        now=NOW,
    )
    assert projects.live_task_count(conn, pid) == 2
    assert projects.open_task_count(conn, pid) == 1
    assert projects.delete_impact(conn, pid) == projects.DeleteImpact(2, 1, 45 * 60)
    assert projects.delete_impact(conn, other) == projects.DeleteImpact(1, 1, 0)

    assert projects.delete(conn, pid) == 2  # the trashed one isn't counted, but goes too
    with pytest.raises(NotFoundError):
        projects.get(conn, pid)
    for gone in (task_id, done, trashed):
        with pytest.raises(NotFoundError):
            tasks.get(conn, gone, include_deleted=True)
    assert conn.execute("SELECT count(*) FROM time_entry").fetchone()[0] == 0
    assert tasks.get(conn, kept).project_id == other  # other projects are untouched
    assert tasks.all_tags(conn) == ["shared"]  # tags only they used are dropped


def test_an_empty_project_deletes_and_general_never_does(conn):
    pid = projects.create(conn, ProjectDraft("Empty"), NOW)
    assert projects.delete(conn, pid) == 0
    with pytest.raises(NotFoundError):
        projects.get(conn, pid)
    with pytest.raises(ValidationError):
        projects.delete(conn, GENERAL_PROJECT_ID)


def test_deleting_stops_a_timer_on_one_of_its_tasks(conn):
    pid = projects.create(conn, ProjectDraft("Timed"), NOW)
    task_id = tasks.create(conn, TaskDraft("t", project_id=pid), NOW)
    timer.start(conn, task_id, NOW)
    projects.delete(conn, pid)
    assert timer.get(conn) is None


def test_unknown_stored_colour_falls_back_to_slate(conn):
    conn.execute("UPDATE project SET color = 'mauve' WHERE id = 1")
    assert projects.get(conn, 1).color is ProjectColor.SLATE
