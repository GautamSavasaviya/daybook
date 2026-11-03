from datetime import time, timedelta

import pytest

from daybook.core.errors import OverlapError, ValidationError
from daybook.core.hold import HoldRequest
from daybook.core.models import HoldReason, PauseReason, TaskDraft, TaskStatus, TimerRun
from daybook.core.timer import RecoveryChoice
from daybook.core.tracking import EntryInput
from daybook.services.events import Entity
from daybook.services.timer import TimerService
from tests.services.test_planning_services import START, TODAY, Env, at


class TimerEnv(Env):
    def __init__(self, conn, db_path) -> None:
        super().__init__(conn, db_path)
        self.timer = TimerService(conn, self.bus, self.clock)

    def new(self, title="Task", **extra) -> int:
        return self.tasks.create(TaskDraft(title=title, **extra))

    def entities(self) -> set[Entity]:
        return {event.entity for event in self.changed}


@pytest.fixture
def env(conn, db_path):
    environment = TimerEnv(conn, db_path)
    yield environment
    environment.worker.shutdown()


def test_start_pause_resume_stop_publish_and_log_time(env):
    task_id = env.new()
    env.changed.clear()
    env.timer.start(task_id)
    assert {Entity.TIMER, Entity.TASK} <= env.entities()
    assert env.tasks.get(task_id).status == TaskStatus.IN_PROGRESS
    assert env.timer.running_task() == task_id

    env.clock.advance(timedelta(minutes=25))
    assert env.timer.tracked_today_s(task_id) == 25 * 60
    env.timer.pause()
    assert env.timer.running_task() is None
    env.clock.advance(timedelta(minutes=5))
    env.timer.resume()
    env.clock.advance(timedelta(minutes=10))
    env.timer.stop()
    assert env.timer.state() is None
    assert [e.seconds // 60 for e in env.timer.entries(task_id)] == [10, 25]
    assert env.timer.tracked_today_s(task_id) == 35 * 60


def test_toggle_is_space(env):
    a, b = env.new("a"), env.new("b")
    env.timer.toggle(a)
    assert env.timer.running_task() == a
    env.clock.advance(timedelta(minutes=1))
    env.timer.toggle(a)
    state = env.timer.state()
    assert state is not None
    assert state.run == TimerRun.PAUSED
    env.timer.toggle(a)
    assert env.timer.running_task() == a
    env.clock.advance(timedelta(minutes=1))
    env.timer.toggle(b)
    assert env.timer.running_task() == b


def test_starting_an_on_hold_task_is_confirmed_first(env):
    task_id = env.new()
    assert env.timer.hold_to_confirm(task_id) is None
    env.tasks.put_on_hold(task_id, HoldRequest(HoldReason.APPROVAL, "review"))
    hold = env.timer.hold_to_confirm(task_id)
    assert hold is not None
    assert hold.reason == HoldReason.APPROVAL
    env.timer.start(task_id)
    assert env.tasks.get(task_id).status == TaskStatus.IN_PROGRESS


def test_done_hold_and_delete_stop_the_timer_and_say_so(env):
    task_id = env.new()
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=10))
    env.changed.clear()
    env.tasks.complete(task_id)
    assert Entity.TIMER in env.entities()
    assert env.timer.state() is None
    assert env.timer.tracked_today_s(task_id) == 600
    # Undo brings the task back, not the timer: the time so far is already saved (D-092).
    env.undo_last()
    assert env.tasks.get(task_id).status == TaskStatus.IN_PROGRESS
    assert env.timer.state() is None

    other = env.new("other")
    env.timer.start(other)
    env.changed.clear()
    env.tasks.complete(task_id)
    assert Entity.TIMER not in env.entities()
    assert env.timer.running_task() == other


def test_recovery_waits_for_an_answer_and_the_heartbeat_waits_too(env, conn):
    task_id = env.new()
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=3))
    env.timer.beat()
    # Simulate a crash: a new service finds the running row.
    env.clock.advance(timedelta(hours=2))
    after_crash = TimerService(conn, env.bus, env.clock)
    pending = after_crash.pending_recovery()
    assert pending is not None
    assert pending.task_id == task_id
    assert pending.last_seen == START + timedelta(minutes=3)
    after_crash.beat()
    state = after_crash.state()
    assert state is not None
    assert state.last_seen == START + timedelta(minutes=3)
    # Quitting without answering leaves the question for the next start.
    after_crash.shutdown()
    assert after_crash.running_task() == task_id

    after_crash.recover(RecoveryChoice.STOP_AT_LAST_SEEN)
    assert after_crash.pending_recovery() is None
    assert after_crash.state() is None
    assert after_crash.tracked_today_s(task_id) == 3 * 60


def test_a_normal_quit_pauses_so_nothing_is_asked_next_time(env, conn):
    task_id = env.new()
    env.timer.start(task_id)
    env.timer.start_heartbeat()
    env.timer.start_heartbeat()  # idempotent
    env.clock.advance(timedelta(minutes=7))
    env.timer.shutdown()
    state = env.timer.state()
    assert state is not None
    assert state.run == TimerRun.PAUSED
    assert TimerService(conn, env.bus, env.clock).pending_recovery() is None
    env.timer.shutdown()  # nothing running: nothing to do


def test_manual_entries_through_the_service(env):
    task_id = env.new()
    first = env.timer.add_entry(task_id, EntryInput(TODAY, time(8, 0), time(9, 0), "early"))
    assert Entity.TIME_ENTRY in env.entities()
    with pytest.raises(OverlapError) as caught:
        env.timer.add_entry(task_id, EntryInput(TODAY, time(8, 30), time(9, 15)))
    assert env.timer.overlap_owner(caught.value) == "Task"
    with pytest.raises(ValidationError):
        env.timer.add_entry(task_id, EntryInput(TODAY, time(9, 0), time(11, 0)))  # future

    env.timer.update_entry(first, EntryInput(TODAY, time(8, 15), time(9, 0), "later"))
    (entry,) = env.timer.entries(task_id)
    assert (entry.start, entry.note) == (at(TODAY, 8, 15), "later")

    env.timer.delete_entry(first)
    assert env.timer.entries(task_id) == []
    assert env.toasts[-1].message == "Removed 45m of logged time"
    env.undo_last()
    (back,) = env.timer.entries(task_id)
    assert (back.start, back.end, back.note) == (entry.start, entry.end, entry.note)


def test_undoing_a_delete_refuses_when_time_was_logged_there_since(env):
    task_id = env.new()
    first = env.timer.add_entry(task_id, EntryInput(TODAY, time(8, 0), time(9, 0)))
    env.timer.delete_entry(first)
    undo = env.toasts[-1].undo
    env.timer.add_entry(task_id, EntryInput(TODAY, time(8, 30), time(9, 0)))
    assert undo is not None
    undo()
    assert env.toasts[-1].message == "Couldn't undo: other time was logged there since"


def test_undoing_a_delete_of_a_purged_task(env, conn):
    task_id = env.new()
    first = env.timer.add_entry(task_id, EntryInput(TODAY, time(8, 0), time(9, 0)))
    env.timer.delete_entry(first)
    conn.execute("DELETE FROM task WHERE id = ?", (task_id,))
    env.undo_last()
    assert env.toasts[-1].message == "Couldn't undo: the task no longer exists"


def test_overlap_with_the_live_timer_names_its_task(env):
    task_id = env.new("Live one", ticket_key="ABC-1")
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=30))
    with pytest.raises(OverlapError) as caught:
        env.timer.add_entry(task_id, EntryInput(TODAY, time(9, 40), time(9, 50)))
    assert caught.value.entry_id is None
    assert env.timer.overlap_owner(caught.value) == "ABC-1"
    caught.value.task_id = 999
    assert env.timer.overlap_owner(caught.value) == ""


def test_day_runs_include_the_live_timer(env):
    task_id = env.new()
    env.timer.add_entry(task_id, EntryInput(TODAY, time(8, 0), time(9, 0)))
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=10))
    (run,) = env.timer.day_runs(TODAY)
    assert (run.start, run.end, run.seconds // 60, run.live) == (
        at(TODAY, 8),
        START + timedelta(minutes=10),
        70,
        True,
    )


# --- Meeting now ----------------------------------------------------------------------------


def test_meeting_now_previews_and_trims_timer_time(env):
    task_id = env.new("Fix login", ticket_key="PROJ-123")
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=20))
    assert env.meetings.adhoc_effect(0).notice == ""
    assert env.meetings.adhoc_effect(12).notice == "This removes 12m from PROJ-123"
    assert "30 minutes" in env.meetings.adhoc_effect(45).error

    env.changed.clear()
    env.meetings.start_now("Call", None, None, started_ago_min=12)
    assert {Entity.MEETING, Entity.TIMER, Entity.TIME_ENTRY} <= env.entities()
    state = env.timer.state()
    assert state is not None
    assert state.pause_reason == PauseReason.MEETING
    assert state.paused_by_meeting_key is not None
    assert env.timer.tracked_today_s(task_id) == 8 * 60


def test_meeting_now_is_refused_over_manual_time(env):
    task_id = env.new("Notes")
    env.timer.add_entry(task_id, EntryInput(TODAY, time(9, 20), time(9, 29)))
    effect = env.meetings.adhoc_effect(15)
    assert effect.error
    assert env.meetings.adhoc_effect(0).error == ""
    with pytest.raises(ValidationError):
        env.meetings.start_now("Call", None, 30, started_ago_min=15)


def test_meeting_now_names_tasks_without_a_key(env):
    task_id = env.new("Write docs")
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=10))
    assert env.meetings.adhoc_effect(5).notice == "This removes 5m from “Write docs”"
    env.tasks.delete(task_id)
    env.clock.advance(timedelta(minutes=1))
    env.timer.start(env.new("next"))
    env.clock.advance(timedelta(minutes=1))
    assert "from “next”" in env.meetings.adhoc_effect(5).notice


# --- Planning -------------------------------------------------------------------------------


def test_the_running_task_is_scheduled_first_and_start_day_finds_the_first_item(env):
    a, b = env.new("a"), env.new("b")
    env.plan.add(a, TODAY, 60)
    env.plan.add(b, TODAY, 60)
    first = env.plan.first_open(TODAY)
    assert first is not None
    assert first.summary.task.id == a
    segment = env.plan.day_plan(TODAY).schedule.segment_at(env.clock.now())
    assert segment is not None
    assert segment.task_id == a
    env.timer.start(b)
    segment = env.plan.day_plan(TODAY).schedule.segment_at(env.clock.now())
    assert segment is not None
    assert segment.task_id == b
    env.tasks.complete(a)
    first = env.plan.first_open(TODAY)
    assert first is not None
    assert first.summary.task.id == b
