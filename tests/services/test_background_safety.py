"""Backups and restore through the service, import, onboarding, login entry and idle."""

import json
import time as systime
from dataclasses import replace
from datetime import time, timedelta

import pytest

from daybook.core.backup import BackupError
from daybook.core.errors import ValidationError
from daybook.core.exchange import ImportRefusedError
from daybook.core.models import EntrySource, ProjectDraft
from daybook.core.onboarding import Choices
from daybook.core.repo import app_state
from daybook.core.repo import reminders as reminder_log
from daybook.core.tracking import EntryInput
from daybook.services import autostart
from daybook.services.backup import LAST_BACKUP, BackupService
from daybook.services.events import AwayDetected, Changed, DayChanged, Entity
from daybook.services.idle import IdleService
from daybook.services.onboarding import OnboardingService
from daybook.services.projects import ProjectService
from tests.services.test_planning_services import START, TODAY
from tests.services.test_timer_service import TimerEnv


class SafetyEnv(TimerEnv):
    def __init__(self, conn, db_path, backups) -> None:
        super().__init__(conn, db_path)
        self.backup = BackupService(
            conn,
            self.bus,
            self.clock,
            directory=backups,
            worker=self.worker,
            settings=self.settings,
            timer=self.timer,
            rollover=self.rollover,
        )
        self.projects = ProjectService(conn, self.bus, self.clock)

    def tasks_titles(self):
        return sorted(r["title"] for r in self.conn.execute("SELECT title FROM task"))


@pytest.fixture
def env(conn, db_path, tmp_path):
    environment = SafetyEnv(conn, db_path, tmp_path / "backups")
    yield environment
    environment.worker.shutdown()


# --- Backups --------------------------------------------------------------------------------


def test_the_daily_backup_runs_once_a_day_on_the_worker(env):
    future = env.backup.run_if_due()
    assert future is not None
    assert env.backup.run_if_due() is None  # already running
    made = future.result(timeout=10)
    env.queue.run()
    assert made.path.exists()
    assert env.backup.last_backup_date() == TODAY
    assert env.backup.run_if_due() is None  # done for today
    assert [b.name for b in env.backup.backups()] == [made.name]
    # The next local day (the day watcher says so) takes another.
    env.clock.advance(timedelta(days=1))
    env.bus.publish(DayChanged(TODAY + timedelta(days=1)))
    for _ in range(200):  # the event queued the backup on the worker
        env.queue.run()
        if len(env.backup.backups()) == 2:
            break
        systime.sleep(0.05)
    assert len(env.backup.backups()) == 2


def test_a_failed_daily_backup_is_reported_and_tried_again(env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise BackupError("disk full")

    monkeypatch.setattr("daybook.services.backup.backup.create", boom)
    future = env.backup.run_if_due()
    assert future is not None
    with pytest.raises(BackupError):
        future.result(timeout=10)
    env.queue.run()
    assert env.backup.last_backup_date() is None
    assert env.backup.run_if_due() is not None  # allowed again


def test_backup_now_and_listing(env):
    env.new("Fix login")
    made = env.backup.backup_now()
    assert env.backup.backups()[0].path == made.path
    assert env.backup.last_backup_date() == TODAY
    assert env.backup.check(made.path).tasks == 1
    assert app_state.get(env.conn, LAST_BACKUP) == TODAY.isoformat()


def test_restore_replaces_the_data_and_refreshes_everything(env):
    env.new("Kept")
    keep = env.new("Kept too")
    env.plan.add(keep, TODAY, 30)
    made = env.backup.backup_now()
    original = replace(env.settings.current)

    env.new("Added later")
    env.settings.save(replace(env.settings.current, work_end=time(17, 0), drift_after_min=9))
    env.timer.start(keep)
    env.clock.advance(timedelta(minutes=20))
    env.timer.beat()
    env.changed.clear()

    checked = env.backup.restore(made.path)
    assert checked.tasks == 2
    assert env.tasks_titles() == ["Kept", "Kept too"]
    assert env.settings.current == original  # the cache was re-read
    assert env.timer.state() is None  # the running timer wasn't in the backup
    # Everything that listens was told, and a safety copy of what was there is kept.
    assert {e.entity for e in env.changed} >= set(Entity)
    safety = [b for b in env.backup.backups() if b.safety]
    assert len(safety) == 1
    assert env.backup.check(safety[0].path).tasks == 3
    assert reminder_log.watermark(env.conn) == env.clock.now()


def test_a_timer_running_in_restored_data_stops_at_its_last_heartbeat(env):
    task = env.new("Timed")
    env.timer.start(task)
    env.clock.advance(timedelta(minutes=10))
    env.timer.beat()
    made = env.backup.backup_now()
    env.clock.advance(timedelta(hours=5))
    env.timer.stop()
    env.backup.restore(made.path)
    assert env.timer.state() is None
    (entry,) = env.timer.entries(task)
    assert entry.seconds == 10 * 60
    assert entry.source == EntrySource.TIMER


def test_a_refused_backup_changes_nothing(env, tmp_path):
    env.new("Safe")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"nonsense" * 1000)
    before = env.tasks_titles()
    with pytest.raises(BackupError):
        env.backup.restore(bad)
    assert env.tasks_titles() == before
    assert env.backup.backups() == []  # no safety copy for a refused file


# --- Export and import ----------------------------------------------------------------------


def test_export_and_import_replace_everything_with_a_safety_copy(env):
    env.new("Exported", ticket_key="ABC-1")
    env.timer.add_entry(env.new("Logged"), EntryInput(TODAY, time(8, 0), time(8, 45)))
    text = env.backup.export_json()
    assert json.loads(text)["format"] == "daybook-export"

    env.new("Later")
    env.changed.clear()
    counts = env.backup.import_json(text)
    assert counts["task"] == 2
    assert env.tasks_titles() == ["Exported", "Logged"]
    assert Entity.TASK in {e.entity for e in env.changed}
    assert len([b for b in env.backup.backups() if b.safety]) == 1
    assert app_state.get(env.conn, "onboarding_done") is True
    assert env.backup.check_import(text)["task"] == 2


def test_a_bad_import_is_refused_before_anything_changes(env):
    env.new("Mine")
    document = json.loads(env.backup.export_json())
    document["tables"]["task"][0]["project_id"] = 999
    with pytest.raises(ImportRefusedError) as caught:
        env.backup.import_json(json.dumps(document))
    assert "tables.task[0]" in str(caught.value)
    assert env.tasks_titles() == ["Mine"]
    assert env.backup.backups() == []
    with pytest.raises(ImportRefusedError):
        env.backup.check_import("not json")


def test_time_entries_csv(env):
    env.timer.add_entry(
        env.new("Logged", ticket_key="ABC-1"), EntryInput(TODAY, time(8, 0), time(9, 0))
    )
    lines = env.backup.time_entries_csv().splitlines()
    assert lines[0].startswith("date,start,end,minutes")
    assert lines[1].endswith("ABC-1,Logged,General,manual,")


# --- Onboarding -----------------------------------------------------------------------------


@pytest.fixture
def first_run(conn, env):
    return OnboardingService(
        conn, env.clock, settings=env.settings, projects=env.projects, tasks=env.tasks
    )


def test_a_fresh_database_is_onboarded_once(env, first_run):
    assert first_run.needed()
    first_run.finish(
        Choices(
            work_start=time(8, 0),
            work_end=time(16, 30),
            workdays=frozenset({0, 1, 2, 3}),
            project_name="PMS",
            key_prefix="PROJ",
            areas=("Docs", "Demos"),
            sample_data=True,
        )
    )
    assert not first_run.needed()
    settings = env.settings.current
    assert (settings.work_start, settings.work_end) == (time(8, 0), time(16, 30))
    assert settings.workdays == frozenset({0, 1, 2, 3})
    names = [p.name for p in env.projects.all()]
    assert {"PMS", "Docs", "Demos"} <= set(names)
    titles = env.tasks_titles()
    assert len(titles) == 3
    assert all(t.startswith("Sample:") for t in titles)
    assert env.conn.execute("SELECT count(*) FROM day_plan_item").fetchone()[0] == 1


def test_a_bad_answer_writes_nothing(env, first_run):
    before = env.settings.current
    with pytest.raises(ValidationError):
        first_run.finish(Choices(work_end=time(7, 0), project_name="PMS", sample_data=True))
    assert env.settings.current == before
    assert [p.name for p in env.projects.all()] == ["General"]
    assert env.tasks_titles() == []
    assert first_run.needed()  # the dialog stays up for a correction
    with pytest.raises(ValidationError):
        first_run.finish(Choices(project_name="PMS", key_prefix="bad prefix!"))
    assert env.settings.current == before


def test_skip_and_existing_data_never_ask(env, first_run):
    first_run.skip()
    assert not first_run.needed()


def test_existing_data_is_never_onboarded(env, first_run):
    env.new("From before this feature")
    assert not first_run.needed()
    assert app_state.get(env.conn, "onboarding_done") is True  # remembered
    env.conn.execute("DELETE FROM task")
    assert not first_run.needed()


def test_other_projects_or_meetings_also_count_as_existing_data(conn, env, first_run):
    env.projects.create(ProjectDraft("Mine"))
    assert not first_run.needed()


# --- Start at login -------------------------------------------------------------------------


def test_the_login_entry_follows_the_setting(tmp_path):
    entry = tmp_path / "config" / "autostart" / "io.github.daybook.Daybook.desktop"
    assert not autostart.is_enabled(entry)
    autostart.apply(entry, "/home/me/.local/bin/daybook", wanted=True)
    text = entry.read_text()
    assert 'Exec="/home/me/.local/bin/daybook" --background' in text
    assert "X-GNOME-Autostart-enabled=true" in text
    assert autostart.is_enabled(entry)
    autostart.apply(entry, "/home/me/.local/bin/daybook", wanted=True)  # no change needed
    autostart.apply(entry, "/opt/daybook/bin/daybook", wanted=True)  # the launcher moved
    assert "/opt/daybook/bin/daybook" in entry.read_text()
    autostart.apply(entry, "/opt/daybook/bin/daybook", wanted=False)
    assert not entry.exists()
    autostart.apply(entry, "x", wanted=False)  # already off: nothing to do
    odd = autostart.entry_text('/home/me/My "Apps"/daybook')
    assert 'Exec="/home/me/My \\"Apps\\"/daybook" --background' in odd


# --- Idle -----------------------------------------------------------------------------------


def test_coming_back_after_being_away_asks_and_discarding_splits_the_time(env):
    idle = IdleService(env.bus, env.clock, settings=env.settings, timer=env.timer)
    asked = []
    env.bus.subscribe(AwayDetected, asked.append)
    task = env.new("Timed")
    env.timer.start(task)
    idle.observe(timedelta(seconds=3), False)
    env.clock.advance(timedelta(minutes=12))
    idle.observe(timedelta(minutes=11), False)  # away since 11 min ago
    env.clock.advance(timedelta(minutes=8))
    idle.observe(timedelta(minutes=19), False)  # still away
    assert asked == []
    env.clock.advance(timedelta(minutes=2))
    idle.observe(timedelta(seconds=1), False)  # back
    (event,) = asked
    assert event.task_id == task
    assert event.start == START + timedelta(minutes=12) - timedelta(minutes=11)
    assert event.end == START + timedelta(minutes=22)

    assert env.timer.discard_away(event.start) == 21 * 60
    (entry,) = env.timer.entries(task)
    assert entry.seconds == 1 * 60
    state = env.timer.state()
    assert state is not None
    assert state.segment_start == START + timedelta(minutes=22)


def test_the_idle_setting_is_read_each_time_and_a_stopped_timer_is_ignored(env):
    idle = IdleService(env.bus, env.clock, settings=env.settings, timer=env.timer)
    asked = []
    env.bus.subscribe(AwayDetected, asked.append)
    env.settings.save(replace(env.settings.current, idle_threshold_min=3))
    task = env.new("Timed")
    env.timer.start(task)
    idle.observe(timedelta(minutes=4), False)
    env.clock.advance(timedelta(minutes=5))
    idle.observe(timedelta(0), False)
    assert len(asked) == 1  # 3 minutes was enough
    env.timer.stop()
    idle.observe(timedelta(minutes=30), False)
    idle.observe(timedelta(0), False)
    assert len(asked) == 1


def test_settings_reload_and_timer_data_replaced_publish(env):
    env.changed.clear()
    env.settings.reload()
    assert Changed(Entity.SETTINGS) in env.changed
    task = env.new("Timed")
    env.timer.start(task)
    env.clock.advance(timedelta(minutes=3))
    env.timer.data_replaced()
    assert env.timer.state() is None
