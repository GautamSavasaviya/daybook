"""Backup/restore round-trip (mandatory, SPEC), JSON import validation, idle and tray."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import backup, exchange, idle, traymenu
from daybook.core.backup import BackupError, Kind
from daybook.core.db import connect
from daybook.core.errors import SchemaTooNewError
from daybook.core.exchange import ImportRefusedError
from daybook.core.migrations import migrate, schema_version
from daybook.core.models import (
    EntrySource,
    Hold,
    HoldReason,
    MeetingDraft,
    ProjectDraft,
    TaskDraft,
)
from daybook.core.repo import app_state, meetings, plan, projects, tasks, time_entries
from daybook.core.repo import timer as timer_repo
from daybook.core.traymenu import Command, TrayStatus

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)
BERLIN = ZoneInfo("Europe/Berlin")


def fill(conn):
    pid = projects.create(conn, ProjectDraft("PMS", key_prefix="PROJ"), NOW)
    task = tasks.create(
        conn, TaskDraft("Fix login", project_id=pid, ticket_key="PROJ-1", tags=("backend",)), NOW
    )
    tasks.add_checklist_item(conn, task, "repro")
    plan.add(conn, task, TODAY, 60)
    time_entries.add_manual(
        conn, task, NOW - timedelta(hours=2), NOW - timedelta(hours=1), note="n", now=NOW
    )
    meetings.create(conn, MeetingDraft("Standup", NOW, 15), "Europe/Berlin", NOW)
    app_state.put(conn, "last_rollover_date", "2026-11-03")
    return pid, task


def dump(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
        for t in (*exchange.TABLES, "app_state")
    }


# --- Backups --------------------------------------------------------------------------------


def test_names_listing_and_rotation(tmp_path):
    assert backup.file_name(NOW) == "daybook-20261103-090000.db"
    assert backup.file_name(NOW, safety=True) == "daybook-20261103-090000-safety.db"
    assert backup.list_backups(tmp_path / "missing") == []
    (tmp_path / "notes.txt").write_text("keep me")
    for day in range(1, 21):
        (tmp_path / backup.file_name(datetime(2026, 10, day, 8, tzinfo=UTC))).write_bytes(b"x")
    for hour in range(5):
        stamp = datetime(2026, 10, 1, hour, tzinfo=UTC)
        (tmp_path / backup.file_name(stamp, safety=True)).write_bytes(b"x")
    listed = backup.list_backups(tmp_path)
    assert listed[0].made_at == datetime(2026, 10, 20, 8, tzinfo=UTC)  # newest first
    removed = backup.rotate(tmp_path)
    assert len(removed) == 6 + 2  # 20 regular → 14, 5 safety → 3
    left = backup.list_backups(tmp_path)
    assert sum(not b.safety for b in left) == 14
    assert sum(b.safety for b in left) == 3
    assert min(b.made_at for b in left if not b.safety) == datetime(2026, 10, 7, 8, tzinfo=UTC)
    assert (tmp_path / "notes.txt").exists()


def test_the_daily_rule():
    assert backup.is_due(None, TODAY)
    assert backup.is_due(TODAY - timedelta(days=1), TODAY)
    assert not backup.is_due(TODAY, TODAY)


def test_a_backup_and_restore_round_trip(conn, tmp_path):
    fill(conn)
    before = dump(conn)
    made = backup.create(conn, tmp_path / "backups", NOW, Kind.MANUAL)
    assert made.name == "daybook-20261103-090000.db"
    assert not list((tmp_path / "backups").glob("*-wal"))
    assert not list((tmp_path / "backups").glob(".*partial"))
    checked = backup.verify(made.path)
    assert (checked.tasks, checked.projects) == (1, 2)

    # The data changes after the backup…
    tasks.create(conn, TaskDraft("Later task"), NOW)
    projects.delete(conn, 2)
    assert dump(conn) != before
    # …and restoring brings back exactly what was saved.
    safety = backup.create(conn, tmp_path / "backups", NOW + timedelta(minutes=1), Kind.SAFETY)
    assert safety.safety
    backup.restore(conn, made.path, NOW + timedelta(minutes=2))
    assert dump(conn) == before
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # The safety copy undoes the restore.
    backup.restore(conn, safety.path, NOW + timedelta(minutes=3))
    assert dump(conn) != before
    assert tasks.get(conn, 2).title == "Later task"


def test_two_backups_in_the_same_second_keep_the_first(conn, tmp_path):
    first = backup.create(conn, tmp_path, NOW, Kind.AUTOMATIC)
    fill(conn)
    again = backup.create(conn, tmp_path, NOW, Kind.AUTOMATIC)
    assert again.path == first.path
    assert backup.verify(first.path).tasks == 0


def test_restoring_an_older_schema_migrates_it(conn, tmp_path):
    old = tmp_path / "old.db"
    raw = connect(old)
    migrate(raw, NOW)
    raw.execute("INSERT INTO app_state VALUES ('k', '1')")
    raw.close()
    backup.restore(conn, old, NOW)
    assert schema_version(conn) >= 1
    assert app_state.get(conn, "k") == 1


@pytest.mark.parametrize(
    ("content", "expect"),
    [
        (
            b"this is not sqlite at all, just text, padded out so that it is long enough" * 4,
            "not a readable",
        ),
    ],
)
def test_verify_refuses_bad_files(tmp_path, content, expect):
    bad = tmp_path / "bad.db"
    bad.write_bytes(content)
    with pytest.raises(BackupError, match=expect):
        backup.verify(bad)
    assert bad.read_bytes() == content  # never modified


def test_verify_refuses_other_databases_and_newer_ones(tmp_path):
    other = tmp_path / "other.db"
    raw = sqlite3.connect(other)
    raw.execute("CREATE TABLE notes (x)")
    raw.close()
    with pytest.raises(BackupError, match="not a Daybook database"):
        backup.verify(other)
    newer = tmp_path / "newer.db"
    raw = connect(newer)
    migrate(raw, NOW)
    raw.execute("PRAGMA user_version = 999")
    raw.close()
    with pytest.raises(SchemaTooNewError):
        backup.verify(newer)
    with pytest.raises(BackupError, match="Can't open"):
        backup.verify(tmp_path / "missing.db")


def test_a_damaged_file_is_refused_and_the_live_data_is_untouched(conn, tmp_path):
    fill(conn)
    before = dump(conn)
    good = backup.create(conn, tmp_path, NOW, Kind.MANUAL).path
    data = bytearray(good.read_bytes())
    for offset in range(4096 * 2, len(data), 97):  # damage pages past the header
        data[offset] ^= 0xFF
    broken = tmp_path / "broken.db"
    broken.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        backup.restore(conn, broken, NOW)
    assert dump(conn) == before


def test_restore_refuses_while_a_transaction_is_open(conn, tmp_path):
    good = backup.create(conn, tmp_path, NOW, Kind.MANUAL).path
    conn.tx_depth = 1
    try:
        with pytest.raises(BackupError, match="busy"):
            backup.restore(conn, good, NOW)
    finally:
        conn.tx_depth = 0


# --- JSON export / import -------------------------------------------------------------------


def test_export_and_import_round_trip(conn, tmp_path):
    _pid, task = fill(conn)
    tasks.put_on_hold(conn, task, _hold(), TODAY, NOW)
    before = dump(conn)
    text = exchange.export_data(conn, NOW)
    document = json.loads(text)
    assert document["format"] == exchange.FORMAT
    assert set(document["tables"]) == set(exchange.TABLES)
    assert len(document["tables"]["task"]) == 1

    checked = exchange.validate(text, NOW)
    assert checked.counts["task"] == 1
    other = connect(tmp_path / "other.db")
    migrate(other, NOW)
    fill_other(other)
    backup.replace_contents(other, checked.conn, NOW)
    checked.close()
    after = dump(other)
    for table in exchange.TABLES:
        assert after[table] == before[table], table
    assert app_state.get(other, "onboarding_done") is True
    other.close()


def _hold():

    return Hold(HoldReason.OTHER, "later", NOW, None)


def fill_other(conn):
    tasks.create(conn, TaskDraft("Will be replaced"), NOW)


def doc(conn):
    return json.loads(exchange.export_data(conn, NOW))


def refused(document):
    with pytest.raises(ImportRefusedError) as caught:
        exchange.validate(json.dumps(document), NOW)
    return [str(p) for p in caught.value.problems]


def test_import_refuses_what_is_not_an_export(conn):
    for text in ("not json", "[1, 2]", '{"format": "other"}'):
        with pytest.raises(ImportRefusedError):
            exchange.validate(text, NOW)
    good = doc(conn)
    assert refused({**good, "format_version": 99})[0].startswith("format_version")
    assert "newer Daybook" in refused({**good, "schema_version": 999})[0]
    missing = {**good, "tables": {k: v for k, v in good["tables"].items() if k != "task"}}
    assert refused(missing) == ["tables.task: Missing table."]
    extra = {**good, "tables": {**good["tables"], "mystery": []}}
    assert refused(extra) == ["tables.mystery: Unknown table."]
    bad_rows = {**good, "tables": {**good["tables"], "task": "nope"}}
    assert refused(bad_rows) == ["tables.task: Expected a list of rows."]
    assert refused({**good, "tables": "x"})[0].startswith("tables")


def test_import_names_the_row_and_the_reason(conn):
    fill(conn)
    good = doc(conn)

    def broken(table, index, **changes):
        copy = json.loads(json.dumps(good))
        copy["tables"][table][index].update(changes)
        return refused(copy)

    assert "refers to something" in broken("task", 0, project_id=999)[0]
    assert broken("task", 0, project_id=999)[0].startswith("tables.task[0]")
    assert "isn't allowed" in broken("task", 0, status="sleeping")[0]
    assert "Unknown column" in broken("task", 0, nope=1)[0]
    assert "Expected text" in broken("task", 0, title=[1])[0]

    copy = json.loads(json.dumps(good))
    entry = copy["tables"]["time_entry"][0]
    copy["tables"]["time_entry"].append({**entry, "id": 77})
    assert "overlaps another time entry" in refused(copy)[0]
    copy = json.loads(json.dumps(good))
    copy["tables"]["task"].append(dict(copy["tables"]["task"][0]))
    assert "appears twice" in refused(copy)[0]
    copy = json.loads(json.dumps(good))
    copy["tables"]["project"] = [p for p in copy["tables"]["project"] if p["id"] != 1]
    assert any("General" in p or "refers" in p for p in refused(copy))
    copy = json.loads(json.dumps(good))
    copy["tables"]["task"].append("junk")
    assert "Expected a row" in refused(copy)[0]


def test_import_reports_many_problems_but_not_all(conn):
    fill(conn)
    good = doc(conn)
    good["tables"]["task"] = [
        {**good["tables"]["task"][0], "id": i, "project_id": 99} for i in range(30)
    ]
    with pytest.raises(ImportRefusedError) as caught:
        exchange.validate(json.dumps(good), NOW)
    assert len(caught.value.problems) == exchange.MAX_PROBLEMS
    assert "tables.task[0]" in str(caught.value)


def test_time_entry_csv(conn):
    fill(conn)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT e.*, t.ticket_key, t.title, p.name AS project_name FROM time_entry e "
            "JOIN task t ON t.id = e.task_id JOIN project p ON p.id = t.project_id"
        )
    ]
    lines = exchange.time_entries_csv(rows, BERLIN).splitlines()
    assert lines[0] == "date,start,end,minutes,ticket,task,project,source,note"
    assert lines[1] == "2026-11-03,08:00:00,09:00:00,60,PROJ-1,Fix login,PMS,manual,n"
    assert len(lines) == 2


# --- Idle -----------------------------------------------------------------------------------

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TEN = timedelta(minutes=10)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_idle_absence_is_reported_once_on_return():
    tracker = idle.IdleTracker(TEN)
    assert tracker.observe(at(0), timedelta(seconds=5), False, True) is None
    assert tracker.observe(at(11), TEN + timedelta(seconds=30), False, True) is None
    assert tracker.away_since == at(11) - TEN - timedelta(seconds=30)
    assert tracker.observe(at(20), timedelta(minutes=19), False, True) is None  # still away
    away = tracker.observe(at(24), timedelta(seconds=1), False, True)
    assert away == idle.Away(at(11) - TEN - timedelta(seconds=30), at(24))
    assert away is not None
    assert away.minutes == 23
    assert tracker.observe(at(25), timedelta(seconds=1), False, True) is None  # only once


def test_a_locked_screen_counts_even_without_the_idle_monitor():
    tracker = idle.IdleTracker(TEN)
    assert tracker.observe(at(0), None, True, True) is None
    assert tracker.observe(at(5), None, True, True) is None
    away = tracker.observe(at(30), None, False, True)
    assert away == idle.Away(at(0), at(30))


def test_a_short_lock_and_a_stopped_timer_ask_nothing():
    tracker = idle.IdleTracker(TEN)
    tracker.observe(at(0), timedelta(0), True, True)
    assert tracker.observe(at(3), timedelta(0), False, True) is None
    tracker.observe(at(10), TEN, False, True)
    assert tracker.away_since is not None
    assert tracker.observe(at(15), TEN * 2, False, False) is None  # timer was stopped
    assert tracker.away_since is None
    assert tracker.observe(at(16), timedelta(0), False, True) is None


# --- Discarding away time on the timer -----------------------------------------------------


def test_discarding_away_time_splits_the_running_segment(conn):
    task = tasks.create(conn, TaskDraft("t"), NOW)
    timer_repo.start(conn, task, at(0))
    assert timer_repo.discard_away(conn, at(30), at(50)) == 20 * 60
    entries = time_entries.for_task(conn, task)
    assert [(e.start, e.end, e.source) for e in entries] == [(at(0), at(30), EntrySource.TIMER)]
    state = timer_repo.get(conn)
    assert state is not None
    assert state.segment_start == at(50)
    # Nothing running, or an absence before the segment began: nothing to discard.
    timer_repo.stop(conn, at(60))
    assert timer_repo.discard_away(conn, at(55), at(58)) == 0
    timer_repo.start(conn, task, at(70))
    assert timer_repo.discard_away(conn, at(60), at(75)) == 0
    assert timer_repo.get(conn).segment_start == at(70)


# --- Tray -----------------------------------------------------------------------------------


def test_tray_text_and_menu():
    none = TrayStatus(None, False, None)
    assert traymenu.title(none) == "Daybook"
    assert traymenu.tooltip(none) == "No timer running"
    running = TrayStatus("PROJ-1 Fix login", True, 23)
    assert traymenu.title(running) == "PROJ-1 Fix login"
    assert traymenu.tooltip(running) == "PROJ-1 Fix login · 23m left · running"
    assert traymenu.tooltip(TrayStatus("X", False, -7)) == "X · 7m over · paused"
    assert traymenu.tooltip(TrayStatus("X", True, None, 95)) == "X · 1h 35m today · running"

    labels = [e.label for e in traymenu.menu(running)]
    assert labels == ["Show Daybook", "Quick Add…", "Meeting Now…", "Pause Timer", "", "Quit"]
    assert traymenu.menu(TrayStatus("X", False, 5))[3].label == "Resume Timer"
    off = traymenu.menu(none)[3]
    assert (off.label, off.enabled) == ("Pause Timer", False)
    assert traymenu.command_for(1, running) == Command.SHOW
    assert traymenu.command_for(4, running) == Command.TOGGLE_TIMER
    assert traymenu.command_for(4, none) is None  # disabled
    assert traymenu.command_for(5, running) is None  # separator
    assert traymenu.command_for(99, running) is None
