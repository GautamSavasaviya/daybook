import sqlite3
from datetime import UTC, datetime

import pytest

from daybook.core.db import connect, transaction
from daybook.core.errors import MigrationError, SchemaTooNewError
from daybook.core.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    migrate,
    schema_version,
)

NOW = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
TS = "2026-11-03T08:00:00Z"

EXPECTED_TABLES = {
    "project", "task", "checklist_item", "tag", "task_tag", "meeting", "meeting_exception",
    "day_plan_item", "time_off", "time_entry", "timer_state", "reminder_log",
    "pending_reminder", "settings", "app_state",
}  # fmt: skip


def tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def test_versions_are_consecutive_from_one():
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    assert MIGRATIONS[-1].version == LATEST_VERSION


def test_migrates_an_empty_database_to_latest(db_path):
    conn = connect(db_path)
    assert schema_version(conn) == 0
    assert migrate(conn, NOW) == LATEST_VERSION
    assert schema_version(conn) == LATEST_VERSION
    assert tables(conn) >= EXPECTED_TABLES
    assert migrate(conn, NOW) == 0  # idempotent
    conn.close()


@pytest.mark.parametrize("start_version", range(LATEST_VERSION))
def test_migrates_from_every_earlier_version(db_path, start_version):
    conn = connect(db_path)
    migrate(conn, NOW, MIGRATIONS[:start_version])
    assert schema_version(conn) == start_version
    migrate(conn, NOW)
    assert schema_version(conn) == LATEST_VERSION
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_refuses_a_newer_database(db_path):
    conn = connect(db_path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    with pytest.raises(SchemaTooNewError) as info:
        migrate(conn, NOW)
    assert info.value.found == LATEST_VERSION + 1
    assert tables(conn) == set()
    conn.close()


def test_failed_migration_is_rolled_back(db_path):
    def broken(conn, _ctx):
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        raise sqlite3.OperationalError("simulated failure")

    conn = connect(db_path)
    with pytest.raises(MigrationError, match="v2"):
        migrate(conn, NOW, (*MIGRATIONS[:1], Migration(2, "broken", broken)))
    assert schema_version(conn) == 1
    assert "half_done" not in tables(conn)
    conn.close()


# --- schema rules ---------------------------------------------------------------------


def add_task(conn, **values):
    row = {"title": "Task", "created_at": TS, "updated_at": TS} | values
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with transaction(conn):
        cur = conn.execute(f"INSERT INTO task ({columns}) VALUES ({marks})", tuple(row.values()))
    return cur.lastrowid


def add_entry(conn, task_id, start, end):
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO time_entry (task_id, start_utc, end_utc, source) "
            "VALUES (?, ?, ?, 'manual')",
            (task_id, start, end),
        )
    return cur.lastrowid


def test_general_area_is_seeded_and_protected(conn):
    row = conn.execute("SELECT name, key_prefix, archived FROM project WHERE id = 1").fetchone()
    assert (row["name"], row["key_prefix"], row["archived"]) == ("General", None, 0)
    with pytest.raises(sqlite3.IntegrityError, match="general_project_protected"):
        conn.execute("DELETE FROM project WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="general_project_protected"):
        conn.execute("UPDATE project SET archived = 1 WHERE id = 1")
    conn.execute("UPDATE project SET name = 'Misc' WHERE id = 1")


def test_new_tasks_default_to_general_p3_todo(conn):
    task_id = add_task(conn)
    row = conn.execute("SELECT project_id, priority, status FROM task WHERE id = ?", (task_id,))
    assert tuple(row.fetchone()) == (1, 3, "todo")


@pytest.mark.parametrize(
    "values",
    [
        {"title": "   "},
        {"priority": 5},
        {"status": "blocked"},
        {"status": "done"},  # done needs completed_at
        {"completed_at": TS},  # completed_at needs done
        {"status": "on_hold"},  # on hold needs a reason and since
        {"hold_reason": "blocked"},  # a reason needs on hold
        {"status": "on_hold", "hold_reason": "approval"},  # missing hold_since
        {"follow_up_date": "2026-11-10"},  # follow-up only while on hold
        {"status": "on_hold", "hold_reason": "waiting", "hold_since": TS},
        {"project_id": 999},
    ],
)
def test_task_constraints_reject_invalid_rows(conn, values):
    with pytest.raises(sqlite3.IntegrityError):
        add_task(conn, **values)


def test_task_on_hold_and_done_rows_are_accepted(conn):
    add_task(
        conn,
        status="on_hold",
        hold_reason="approval",
        hold_since=TS,
        hold_note="Waiting for PM sign-off",
        follow_up_date="2026-11-06",
    )
    add_task(conn, status="done", completed_at=TS)


def test_time_entries_may_touch_but_not_overlap(conn):
    task = add_task(conn)
    add_entry(conn, task, "2026-11-03T09:00:00Z", "2026-11-03T10:00:00Z")
    add_entry(conn, task, "2026-11-03T10:00:00Z", "2026-11-03T10:30:00Z")  # adjacent is fine
    add_entry(conn, task, "2026-11-03T08:00:00Z", "2026-11-03T09:00:00Z")
    for start, end in [
        ("2026-11-03T09:30:00Z", "2026-11-03T09:45:00Z"),  # inside
        ("2026-11-03T07:00:00Z", "2026-11-03T12:00:00Z"),  # covers several
        ("2026-11-03T10:29:59Z", "2026-11-03T11:00:00Z"),  # overlaps the end by 1 s
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="time_entry_overlap"):
            add_entry(conn, task, start, end)


def test_time_entry_update_checks_overlap_except_itself(conn):
    task = add_task(conn)
    first = add_entry(conn, task, "2026-11-03T09:00:00Z", "2026-11-03T10:00:00Z")
    add_entry(conn, task, "2026-11-03T11:00:00Z", "2026-11-03T12:00:00Z")
    conn.execute("UPDATE time_entry SET end_utc = '2026-11-03T11:00:00Z' WHERE id = ?", (first,))
    with pytest.raises(sqlite3.IntegrityError, match="time_entry_overlap"):
        conn.execute(
            "UPDATE time_entry SET end_utc = '2026-11-03T11:30:00Z' WHERE id = ?", (first,)
        )


def test_time_entry_must_end_after_it_starts(conn):
    task = add_task(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_entry(conn, task, "2026-11-03T10:00:00Z", "2026-11-03T10:00:00Z")


def test_rest_of_day_plan_items_and_positive_allotments(conn):
    task = add_task(conn)
    conn.execute(
        "INSERT INTO day_plan_item (plan_date, task_id, allotted_min, position) "
        "VALUES ('2026-11-03', ?, NULL, 0)",
        (task,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO day_plan_item (plan_date, task_id, allotted_min, position) "
            "VALUES ('2026-11-04', ?, 0, 0)",
            (task,),
        )


def test_timer_state_is_a_consistent_singleton(conn):
    task = add_task(conn)
    insert = (
        "INSERT INTO timer_state (id, task_id, state, segment_start_utc, last_seen_utc, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (1, task, "running", None, TS, TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (2, task, "paused", None, TS, TS))
    conn.execute(insert, (1, task, "running", TS, TS, TS))


def test_open_ended_meetings_must_be_one_off_adhoc(conn):
    insert = (
        "INSERT INTO meeting (title, start_utc, duration_min, tzid, reminder_offsets, "
        "is_adhoc, open_ended, recurrence, created_at, updated_at) "
        "VALUES ('Sync', ?, 30, 'Europe/Berlin', '[10, 1]', ?, ?, ?, ?, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (TS, 0, 1, None, TS, TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (TS, 1, 1, '{"freq": "daily"}', TS, TS))
    conn.execute(insert, (TS, 1, 1, None, TS, TS))


def test_deleting_a_task_cascades_to_its_rows(conn):
    task = add_task(conn)
    add_entry(conn, task, "2026-11-03T09:00:00Z", "2026-11-03T10:00:00Z")
    conn.execute(
        "INSERT INTO checklist_item (task_id, text, position) VALUES (?, 'step', 0)", (task,)
    )
    conn.execute("DELETE FROM task WHERE id = ?", (task,))
    assert conn.execute("SELECT count(*) FROM time_entry").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM checklist_item").fetchone()[0] == 0
