"""Regression tests for bugs found in the Phase 8 code review (each one failed before)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from daybook.core import backup, exchange, hold, quickadd, timeutil
from daybook.core.calendar import WorkCalendar
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.hold import HoldRequest
from daybook.core.models import TaskDraft
from daybook.core.quickadd import ParseContext
from daybook.core.repo import reporting, tasks, time_entries

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
CTX = ParseContext(date(2026, 11, 3), (), WorkCalendar(frozenset(range(5))))


def test_quick_add_never_raises_on_a_huge_ticket_token():
    result = quickadd.parse("A-" + "9" * 80 + " write it", CTX)  # used to raise
    assert not result.ok
    assert "Keep it under" in result.errors[0]
    assert result.parsed.title == "write it"


@pytest.mark.parametrize("name", ["daybook-20261399-999999.db", "daybook-20260230-010101.db"])
def test_a_stray_file_that_looks_like_a_backup_does_not_break_listing(tmp_path, name):
    (tmp_path / name).write_bytes(b"x")
    (tmp_path / backup.file_name(NOW)).write_bytes(b"x")
    assert [b.name for b in backup.list_backups(tmp_path)] == [backup.file_name(NOW)]
    backup.rotate(tmp_path)  # must not raise either


def test_csv_cells_that_look_like_formulas_are_kept_as_text():
    assert exchange.csv_safe("=SUM(A1:A9)") == "'=SUM(A1:A9)"
    assert exchange.csv_safe("@cmd") == "'@cmd"
    assert exchange.csv_safe("-1+1") == "'-1+1"
    assert exchange.csv_safe("+44 20") == "'+44 20"
    assert exchange.csv_safe("plain") == "plain"
    assert exchange.csv_safe(42) == 42
    rows = [
        {
            "start_utc": "2026-11-03T08:00:00Z",
            "end_utc": "2026-11-03T09:00:00Z",
            "source": "manual",
            "note": "=1+1",
            "ticket_key": None,
            "title": "@home",
            "project_name": "Docs",
        }
    ]
    line = exchange.time_entries_csv(rows, UTC).splitlines()[1]
    assert "'@home" in line
    assert "'=1+1" in line


def test_reports_and_timesheets_ignore_time_on_deleted_tasks(conn):
    task = tasks.create(conn, TaskDraft("gone soon"), NOW)
    time_entries.add_manual(
        conn, task, NOW - timedelta(hours=2), NOW - timedelta(hours=1), note="", now=NOW
    )
    start, end = NOW - timedelta(days=1), NOW
    assert len(reporting.slices(conn, start, end, NOW)) == 1
    tasks.soft_delete(conn, task, NOW)
    assert reporting.slices(conn, start, end, NOW) == []
    assert reporting.all_time_entries(conn) == []


def test_an_entry_of_a_deleted_task_cannot_be_edited(conn):
    task = tasks.create(conn, TaskDraft("t"), NOW)
    entry = time_entries.add_manual(
        conn, task, NOW - timedelta(hours=2), NOW - timedelta(hours=1), note="", now=NOW
    )
    tasks.soft_delete(conn, task, NOW)
    with pytest.raises(NotFoundError):
        time_entries.update(
            conn, entry, NOW - timedelta(hours=2), NOW - timedelta(hours=1), note="x", now=NOW
        )


def test_a_bad_hold_reason_is_a_validation_error():
    with pytest.raises(ValidationError) as caught:
        hold.validate(HoldRequest("sleepy", None), date(2026, 11, 3))  # type: ignore[arg-type]
    assert caught.value.field == "hold_reason"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(29, "now"), (30, "in 1 min"), (89, "in 1 min"), (90, "in 2 min"), (150, "in 3 min"),
     (-150, "3 min ago"), (3600, "in 1h 00m")],
)  # fmt: skip
def test_relative_times_round_halves_up(seconds, text):
    assert timeutil.fmt_relative(timedelta(seconds=seconds)) == text
