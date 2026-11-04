from datetime import timedelta

import pytest

from daybook.core.models import MeetingDraft, PauseReason, TaskStatus
from daybook.core.reminders import Action, Kind, Tier, View
from daybook.core.repo import reminders as log_repo
from daybook.services import notifier as notifier_module
from daybook.services.events import ReminderGone, ReminderShown, ShowView
from daybook.services.notifier import Notifier
from daybook.services.power import PowerMonitor
from daybook.services.scheduler import ReminderScheduler
from tests.services.test_planning_services import TODAY, at
from tests.services.test_timer_service import TimerEnv


class FakeNotifier(Notifier):
    def __init__(self, bus, settings) -> None:
        super().__init__(bus, settings)
        self.sent: list[tuple[str, str]] = []  # (key, title)
        self.withdrawn: list[str] = []
        self.messages = {}

    def show(self, reminder, message) -> None:
        super().show(reminder, message)
        self.sent.append((reminder.key, message.title))
        self.messages[reminder.key] = message

    def withdraw(self, subject) -> None:
        super().withdraw(subject)
        self.withdrawn.append(subject)


class SchedulerEnv(TimerEnv):
    def __init__(self, conn, db_path) -> None:
        super().__init__(conn, db_path)
        self.notifier = FakeNotifier(self.bus, self.settings)
        self.scheduler = self.make_scheduler()
        self.views: list[View] = []
        self.cards: list[ReminderShown] = []
        self.gone: list[str] = []
        self.bus.subscribe(ShowView, lambda e: self.views.append(e.view))
        self.bus.subscribe(ReminderShown, self.cards.append)
        self.bus.subscribe(ReminderGone, lambda e: self.gone.append(e.subject))

    def make_scheduler(self) -> ReminderScheduler:
        return ReminderScheduler(
            self.conn,
            self.bus,
            self.clock,
            settings=self.settings,
            calendar=self.calendar,
            tasks=self.tasks,
            plan=self.plan,
            meetings=self.meetings,
            timer=self.timer,
            notifier=self.notifier,
        )

    def go(self, **delta) -> None:
        self.clock.advance(timedelta(**delta))
        self.scheduler.recompute()

    def keys(self, kind: Kind | None = None) -> list[str]:
        return [k for k, _t in self.notifier.sent if kind is None or k.startswith(kind.value)]


@pytest.fixture
def env(conn, db_path):
    environment = SchedulerEnv(conn, db_path)
    yield environment
    environment.scheduler.stop()
    environment.worker.shutdown()


def standup(env, hour=10, minute=0, minutes=15, link="https://meet/s") -> int:
    return env.meetings.create(
        MeetingDraft("Standup", at(TODAY, hour, minute), minutes, link, reminder_offsets=(10, 1))
    )


def test_a_new_database_fires_nothing_old_and_then_fires_on_time_once(env):
    # 09:30 in Berlin; the start-of-day reminder (09:00) is before the first check.
    standup(env)
    env.scheduler.start()
    assert env.notifier.sent == []
    env.go(minutes=19)  # 09:49
    assert env.keys(Kind.MEETING) == []
    env.go(minutes=1)  # 09:50
    assert [t for _k, t in env.notifier.sent] == ["Standup in 10 min"]
    assert env.cards[-1].tier == Tier.STAY
    env.scheduler.recompute()
    assert len(env.keys(Kind.MEETING)) == 1
    # A second scheduler on the same database (a restart) doesn't fire it again.
    again = env.make_scheduler()
    again.start()
    assert len(env.keys(Kind.MEETING)) == 1
    again.stop()


def test_missed_while_closed_gives_one_summary_and_one_live_meeting(env):
    standup(env, hour=10, minutes=30)
    env.scheduler.start()
    env.scheduler.stop()
    # The app is closed from 09:30 until 10:04.
    env.clock.advance(timedelta(minutes=34))
    later = env.make_scheduler()
    later.start()
    titles = [t for _k, t in env.notifier.sent]
    assert titles == ["Standup started 4 min ago"]
    missed = env.conn.execute("SELECT key FROM reminder_log WHERE status = 'missed'").fetchall()
    assert len(missed) == 1  # the 10-minute offset, folded into the live reminder
    later.stop()


def test_a_meeting_that_ended_while_closed_is_in_the_summary(env):
    standup(env, hour=10, minutes=15)
    env.scheduler.start()
    env.scheduler.stop()
    env.clock.advance(timedelta(hours=2))
    later = env.make_scheduler()
    later.start()
    titles = [t for _k, t in env.notifier.sent]
    assert titles == ["While you were away"]
    summary_key = env.notifier.sent[0][0]
    body = env.notifier.messages[summary_key].body
    assert body.startswith("Standup 10:00")
    assert body.endswith("(missed)")
    later.stop()


def test_time_is_up_then_extend_and_mark_done(env):
    task_id = env.new("Fix login", ticket_key="PROJ-123")
    env.plan.add(task_id, TODAY, 30)
    env.scheduler.start()
    env.timer.start(task_id)
    env.go(minutes=20)  # 10 min left
    assert env.keys(Kind.ALLOT_WARN)
    env.go(minutes=10)
    (key,) = env.keys(Kind.ALLOT_UP)
    assert env.notifier.messages[key].title == "Time's up: PROJ-123"

    env.scheduler.handle(key, Action.EXTEND)
    assert f"task:{task_id}" in env.notifier.withdrawn
    item = env.plan.entries(TODAY)[0].item
    assert item.allotted_min == 45
    env.go(minutes=15)
    assert len(env.keys(Kind.ALLOT_UP)) == 2  # a fresh one for the new allotment
    env.scheduler.handle(env.keys(Kind.ALLOT_UP)[-1], Action.DONE)
    assert env.tasks.get(task_id).status == TaskStatus.DONE
    assert env.timer.state() is None
    logged = env.conn.execute(
        "SELECT action FROM reminder_log WHERE key = ?", (env.keys(Kind.ALLOT_UP)[-1],)
    ).fetchone()
    assert logged["action"] == "done"


def test_switch_to_next_starts_the_next_planned_task(env):
    first = env.new("first")
    second = env.new("second")
    env.plan.add(first, TODAY, 15)
    env.plan.add(second, TODAY, 30)
    env.scheduler.start()
    env.timer.start(first)
    env.go(minutes=15)
    (key,) = env.keys(Kind.ALLOT_UP)
    env.scheduler.handle(key, Action.SWITCH)
    assert env.timer.running_task() == second


def test_drift_nudge_start_and_skip(env):
    a, b = env.new("a"), env.new("b")
    env.plan.add(a, TODAY, 60)
    env.plan.add(b, TODAY, 60)
    env.scheduler.start()  # 09:30, nothing tracked today
    env.go(minutes=1)
    (key,) = env.keys(Kind.DRIFT)
    assert env.notifier.messages[key].title == "Planned now: a"
    env.scheduler.handle(key, Action.SKIP)
    assert [e.summary.task.id for e in env.plan.entries(TODAY)] == [b, a]
    env.go(minutes=6)
    (_, second) = env.keys(Kind.DRIFT)
    env.scheduler.handle(second, Action.START)
    assert env.timer.running_task() == b


def test_a_drift_nudge_is_withdrawn_when_a_timer_starts(env):
    task_id = env.new("a")
    env.plan.add(task_id, TODAY, 60)
    env.scheduler.start()
    env.go(minutes=1)
    assert env.keys(Kind.DRIFT)
    env.timer.start(task_id)
    env.scheduler.recompute()
    assert f"task:{task_id}" in env.notifier.withdrawn
    assert f"task:{task_id}" in env.gone


def test_snooze_fires_again_two_minutes_later(env):
    standup(env)
    env.scheduler.start()
    env.go(minutes=20)  # 09:50
    (key,) = env.keys(Kind.MEETING)
    env.scheduler.handle(key, Action.SNOOZE)
    env.go(minutes=1)
    assert len(env.keys(Kind.MEETING)) == 1
    env.go(minutes=1)
    assert env.keys(Kind.MEETING)[-1] == key + ":snooze1"
    env.scheduler.handle(key + ":snooze1", Action.SNOOZE)
    env.go(minutes=2)
    assert env.keys(Kind.MEETING)[-1] == key + ":snooze2"


def test_autopause_then_back_to_the_task(env):
    task_id = env.new("Fix login", ticket_key="PROJ-123")
    standup(env)
    env.scheduler.start()
    env.timer.start(task_id)
    env.go(minutes=30)  # 10:00
    state = env.timer.state()
    assert state is not None
    assert state.pause_reason == PauseReason.MEETING
    env.go(minutes=15)  # 10:15, the standup ends
    (key,) = env.keys(Kind.MEETING_END)
    assert env.notifier.messages[key].title == "Back to PROJ-123?"
    env.scheduler.handle(key, Action.RESUME)
    assert env.timer.running_task() == task_id


def test_overflow_once_then_move_to_tomorrow(env):
    for title in ("a", "b", "c"):
        env.plan.add(env.new(title), TODAY, 180)
    env.scheduler.start()
    (key,) = env.keys(Kind.OVERFLOW)
    env.scheduler.recompute()
    assert len(env.keys(Kind.OVERFLOW)) == 1
    env.scheduler.handle(key, Action.MOVE_TOMORROW)
    assert env.plan.day_plan(TODAY).schedule.overflow == ()


def test_open_meeting_after_a_restart_can_end_at_the_last_activity(env):
    env.scheduler.start()
    meeting_id = env.meetings.start_now("Call", None, None)
    env.go(minutes=10)
    env.scheduler.stop()
    env.clock.advance(timedelta(hours=1))
    later = env.make_scheduler()
    later.start()
    (key,) = env.keys(Kind.OPEN_MEETING)
    later.handle(key, Action.END_AT_LAST)
    assert env.meetings.get(meeting_id).duration_min == 10
    later.stop()


def test_open_view_unknown_keys_and_the_test_reminder(env):
    env.scheduler.start()
    env.scheduler.send_test()
    (key,) = env.keys(Kind.TEST)
    env.scheduler.handle(key, Action.OPEN)
    assert env.views == [View.TODAY]
    env.scheduler.handle("nope", Action.OPEN)
    env.scheduler.handle(key, "not-an-action")
    env.scheduler.handle(key, Action.WORKS)


def test_join_opens_the_link_and_errors_become_toasts(env, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "daybook.services.scheduler.Gio.AppInfo.launch_default_for_uri",
        lambda uri, _ctx: opened.append(uri),
    )
    standup(env)
    env.scheduler.start()
    env.go(minutes=20)
    (key,) = env.keys(Kind.MEETING)
    env.scheduler.handle(key, Action.JOIN)
    assert opened == ["https://meet/s"]
    # Acting on a task that has gone since shows a toast instead of failing.
    task_id = env.new("gone")
    env.plan.add(task_id, TODAY, 15)
    env.timer.start(task_id)
    env.go(minutes=15)
    up = env.keys(Kind.ALLOT_UP)[-1]
    env.tasks.delete(task_id)
    env.conn.execute("DELETE FROM task WHERE id = ?", (task_id,))
    env.scheduler.handle(up, Action.DONE)
    assert env.toasts[-1].message == "That is no longer there"


def test_watermark_and_payloads_survive(env, conn):
    env.scheduler.start()
    assert log_repo.watermark(conn) == env.clock.now()
    env.scheduler.send_test()
    (key,) = env.keys(Kind.TEST)
    assert log_repo.get(conn, key) is not None
    assert log_repo.get(conn, "missing") is None


def test_power_monitor_routes_logind_signals():
    calls = []
    monitor = PowerMonitor(
        on_sleep=lambda: calls.append("sleep"),
        on_resume=lambda: calls.append("resume"),
        on_shutdown=lambda: calls.append("shutdown"),
    )
    monitor.handle("PrepareForSleep", True)
    monitor.handle("PrepareForSleep", False)
    monitor.handle("PrepareForShutdown", True)
    monitor.handle("PrepareForShutdown", False)
    monitor.handle("Other", True)
    assert calls == ["sleep", "resume", "shutdown"]
    monitor.stop()


def test_diagnostics_explain_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier_module, "_data_dirs", lambda: [tmp_path])
    monkeypatch.setattr(notifier_module, "_notification_server", lambda: False)
    problems = notifier_module.diagnose("io.example.App", devel=False)
    assert len(problems) == 2
    assert "not installed" in problems[0]
    (tmp_path / "applications").mkdir()
    (tmp_path / "applications" / "io.example.App.desktop").write_text("")
    problems = notifier_module.diagnose("io.example.App", devel=False)
    assert "D-Bus service file" in problems[0]
    assert len(notifier_module.diagnose("io.example.App", devel=True)) == 1
    monkeypatch.setattr(notifier_module, "_notification_server", lambda: True)
    assert notifier_module.diagnose("io.example.App", devel=True) == []


class FakeApp:
    def __init__(self) -> None:
        self.sent = []
        self.withdrawn = []

    def send_notification(self, notification_id, notification) -> None:
        self.sent.append((notification_id, notification))

    def withdraw_notification(self, notification_id) -> None:
        self.withdrawn.append(notification_id)


def test_the_notifier_uses_the_subject_as_id_and_plays_by_tier(env, monkeypatch):
    played = []

    class Sounds:
        def play_simple(self, attrs, _cancellable):
            played.append(attrs["event.id"])

    monkeypatch.setattr(notifier_module, "_load_gsound", Sounds)
    app = FakeApp()
    real = Notifier(env.bus, env.settings)
    real.attach(app)
    env.scheduler._notifier = real
    env.scheduler.start()
    env.scheduler.send_test()
    assert app.sent[0][0] == "test"
    assert played == ["alarm-clock-elapsed"]
    real.withdraw("test")
    assert app.withdrawn == ["test"]
