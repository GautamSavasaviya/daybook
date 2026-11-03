import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from gi.repository import GLib

from daybook.core.clock import ManualClock
from daybook.core.errors import ValidationError
from daybook.core.hold import HoldRequest
from daybook.core.models import (
    HoldReason,
    PlanRequest,
    Priority,
    ProjectDraft,
    TaskDraft,
    TaskStatus,
)
from daybook.core.reschedule import MoveChoice, MoveKind
from daybook.core.tasklists import ListKind, TaskQuery
from daybook.services import day
from daybook.services.calendar import CalendarService
from daybook.services.day import DayWatcher
from daybook.services.events import Changed, DayChanged, Entity, EventBus, Toast
from daybook.services.projects import ProjectService
from daybook.services.settings import SettingsService
from daybook.services.tasks import TaskService
from daybook.services.text import short
from daybook.services.worker import Worker

BERLIN = ZoneInfo("Europe/Berlin")
# Tuesday 3 Nov 2026, 10:00 in Berlin.
START = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)


class Queue:
    def __init__(self) -> None:
        self.callbacks = []
        self.lock = threading.Lock()

    def __call__(self, callback) -> None:
        with self.lock:
            self.callbacks.append(callback)

    def run(self) -> None:
        with self.lock:
            pending, self.callbacks = self.callbacks, []
        for callback in pending:
            callback()


class Env:
    def __init__(self, conn, db_path) -> None:
        self.clock = ManualClock(START, tz=BERLIN)
        self.queue = Queue()
        self.bus = EventBus(self.queue)
        self.settings = SettingsService(conn, self.bus)
        self.worker = Worker(db_path, self.queue)
        self.calendar = CalendarService(conn, self.settings, self.bus)
        self.tasks = TaskService(conn, self.bus, self.clock, self.worker, self.calendar)
        self.projects = ProjectService(conn, self.bus, self.clock)
        self.changed: list[Changed] = []
        self.toasts: list[Toast] = []
        self.bus.subscribe(Changed, self.changed.append)
        self.bus.subscribe(Toast, self.toasts.append)

    def entities(self):
        return [event.entity for event in self.changed]

    def undo_last(self) -> None:
        undo = self.toasts[-1].undo
        assert undo is not None
        undo()


@pytest.fixture
def env(conn, db_path):
    environment = Env(conn, db_path)
    yield environment
    environment.worker.shutdown()


# --- Creating and editing ---------------------------------------------------------------


def test_create_publishes_task_and_plan_changes(env):
    plain = env.tasks.create(TaskDraft("Plain"))
    assert env.changed == [Changed(Entity.TASK, frozenset({plain}))]
    env.changed.clear()
    planned = env.tasks.create(TaskDraft("Planned", plan=PlanRequest(TODAY, 30)))
    assert env.changed == [Changed(Entity.TASK, frozenset({planned})), Changed(Entity.PLAN)]
    assert [i.plan_date for i in env.tasks.plan_items(planned)] == [TODAY]


def test_create_refuses_planning_in_the_past(env):
    with pytest.raises(ValidationError) as info:
        env.tasks.create(TaskDraft("x", plan=PlanRequest(TODAY - timedelta(days=1), 30)))
    assert info.value.field == "plan"


def test_update_and_tags(env):
    task_id = env.tasks.create(TaskDraft("Old"))
    saved = env.tasks.update(replace(env.tasks.get(task_id), title="New"))
    assert saved.title == "New"
    env.tasks.set_tags(task_id, ["ui", "api"])
    assert env.tasks.tags(task_id) == ("api", "ui")
    assert env.tasks.all_tags() == ["api", "ui"]
    assert env.tasks.tag_suggestions("#a") == ["api"]


def test_suggestions_and_ticket_key_lookup(env):
    env.tasks.create(TaskDraft("a", requested_by="Rahul Shah", ticket_key="PROJ-1"))
    env.tasks.create(TaskDraft("b", requested_by="Priya"))
    assert env.tasks.requested_by_suggestions("sha") == ["Rahul Shah"]
    assert set(env.tasks.requested_by_names()) == {"Rahul Shah", "Priya"}
    assert env.tasks.ticket_key_in_use("proj-1")


# --- Status changes with Undo -----------------------------------------------------------


def test_complete_toasts_and_undo_restores(env):
    task_id = env.tasks.create(TaskDraft("Fix login", plan=PlanRequest(TODAY, 30)))
    env.tasks.complete(task_id)
    assert env.tasks.get(task_id).status is TaskStatus.DONE
    assert env.toasts[-1].message == "Completed “Fix login”"
    env.undo_last()
    assert env.tasks.get(task_id).status is TaskStatus.TO_DO
    assert len(env.tasks.plan_items(task_id)) == 1
    count = len(env.toasts)
    env.tasks.complete(task_id)
    env.tasks.complete(task_id)  # already done: no second toast
    assert len(env.toasts) == count + 1


def test_set_status_routes_done_and_refuses_on_hold(env):
    task_id = env.tasks.create(TaskDraft("x"))
    env.tasks.set_status(task_id, TaskStatus.IN_PROGRESS)
    assert env.tasks.get(task_id).status is TaskStatus.IN_PROGRESS
    env.tasks.set_status(task_id, TaskStatus.DONE)
    assert env.toasts[-1].message.startswith("Completed")
    env.tasks.reopen(task_id)
    assert env.tasks.get(task_id).status is TaskStatus.TO_DO
    with pytest.raises(ValueError, match="put_on_hold"):
        env.tasks.set_status(task_id, TaskStatus.ON_HOLD)


def test_hold_takes_the_task_out_of_the_plan_and_undo_puts_it_back(env):
    task_id = env.tasks.create(TaskDraft("Deploy", plan=PlanRequest(TODAY, 60)))
    env.changed.clear()
    env.tasks.put_on_hold(task_id, HoldRequest(HoldReason.APPROVAL, " QA ", TODAY))
    task = env.tasks.get(task_id)
    assert task.status is TaskStatus.ON_HOLD
    assert task.hold is not None
    assert (task.hold.note, task.hold.since) == ("QA", START)
    assert env.tasks.plan_items(task_id) == []
    assert Entity.PLAN in env.entities()
    assert env.toasts[-1].message == "“Deploy” is on hold"
    env.undo_last()
    assert env.tasks.get(task_id).status is TaskStatus.TO_DO
    assert [i.allotted_min for i in env.tasks.plan_items(task_id)] == [60]


def test_hold_validates_the_follow_up(env):
    task_id = env.tasks.create(TaskDraft("x"))
    with pytest.raises(ValidationError):
        env.tasks.put_on_hold(
            task_id, HoldRequest(HoldReason.OTHER, None, TODAY - timedelta(days=1))
        )
    assert env.tasks.get(task_id).status is TaskStatus.TO_DO


def test_resume_sets_in_progress(env):
    task_id = env.tasks.create(TaskDraft("x"))
    env.tasks.put_on_hold(task_id, HoldRequest(HoldReason.BLOCKED))
    env.tasks.resume(task_id)
    task = env.tasks.get(task_id)
    assert (task.status, task.hold) == (TaskStatus.IN_PROGRESS, None)


# --- Move to… ---------------------------------------------------------------------------


def test_move_to_tomorrow_with_reason_and_undo(env):
    task_id = env.tasks.create(TaskDraft("Report", plan=PlanRequest(TODAY, 45)))
    choice = env.tasks.quick_move_choice()
    assert choice.kind is MoveKind.TOMORROW
    target = env.tasks.move(task_id, choice, "waiting for data")
    assert target == date(2026, 11, 4)
    task = env.tasks.get(task_id)
    assert task.moved_count == 1
    assert task.notes == "3 Nov: moved to tomorrow — waiting for data"
    assert env.toasts[-1].message == "Moved “Report” to tomorrow"
    env.undo_last()
    task = env.tasks.get(task_id)
    assert (task.moved_count, task.notes) == (0, "")
    assert [i.plan_date for i in env.tasks.plan_items(task_id)] == [TODAY]


def test_move_on_friday_offers_the_next_working_day(env):
    env.clock.advance(timedelta(days=3))  # Friday 6 Nov
    task_id = env.tasks.create(TaskDraft("x"))
    choice = env.tasks.quick_move_choice()
    assert choice.kind is MoveKind.NEXT_WORKING_DAY
    assert env.tasks.move(task_id, choice) == date(2026, 11, 9)


def test_move_to_unplanned_and_refusals(env):
    task_id = env.tasks.create(TaskDraft("x", plan=PlanRequest(TODAY, 30)))
    assert env.tasks.move(task_id, MoveChoice(MoveKind.UNPLANNED)) is None
    assert env.toasts[-1].message == "“x” is unplanned"
    with pytest.raises(ValidationError):
        env.tasks.move(task_id, MoveChoice(MoveKind.DATE, date(2026, 11, 7)))  # Saturday
    env.tasks.complete(task_id)
    with pytest.raises(ValidationError) as info:
        env.tasks.move(task_id, MoveChoice(MoveKind.TOMORROW))
    assert info.value.field == "move"


def test_move_respects_workday_settings(env):
    env.settings.save(replace(env.settings.current, workdays=frozenset({0, 1, 2, 3, 4, 5})))
    env.clock.advance(timedelta(days=3))  # Friday; Saturday is now a workday
    task_id = env.tasks.create(TaskDraft("x"))
    assert env.tasks.move_target(env.tasks.quick_move_choice()) == date(2026, 11, 7)
    assert env.tasks.move(task_id, MoveChoice(MoveKind.DATE, date(2026, 11, 7))) == date(
        2026, 11, 7
    )


# --- Delete and purge -------------------------------------------------------------------


def test_delete_with_undo_and_purge_after_a_day(env, conn):
    task_id = env.tasks.create(TaskDraft("Temp"))
    assert env.tasks.logged_seconds(task_id) == 0
    env.tasks.delete(task_id)
    assert env.toasts[-1].message == "Deleted “Temp”"
    env.undo_last()
    assert env.tasks.get(task_id).deleted_at is None
    env.tasks.delete(task_id)
    env.clock.advance(timedelta(hours=23))
    assert env.tasks.purge_deleted() == 0
    env.clock.advance(timedelta(hours=2))
    assert env.tasks.purge_deleted() == 1
    env.undo_last()  # too late: the task is gone
    assert env.toasts[-1].message == "Couldn't undo: the task no longer exists"


# --- Checklist --------------------------------------------------------------------------


def test_checklist_operations_and_undo_of_a_removed_item(env):
    task_id = env.tasks.create(TaskDraft("x"))
    item = env.tasks.add_checklist_item(task_id, "repro")
    env.tasks.set_checklist_done(item, True)
    env.tasks.rename_checklist_item(item, "reproduce")
    env.tasks.delete_checklist_item(item)
    assert env.tasks.checklist(task_id) == []
    assert env.toasts[-1].message == "Removed “reproduce”"
    env.undo_last()
    [restored] = env.tasks.checklist(task_id)
    assert (restored.text, restored.done) == ("reproduce", True)


def test_undoing_a_checklist_removal_after_the_task_was_purged(env):
    task_id = env.tasks.create(TaskDraft("x"))
    env.tasks.delete_checklist_item(env.tasks.add_checklist_item(task_id, "step"))
    env.tasks.delete(task_id)
    env.clock.advance(timedelta(days=2))
    env.tasks.purge_deleted()
    env.toasts[-2].undo()
    assert env.toasts[-1].message == "Couldn't undo: the task no longer exists"


# --- Loading on the worker --------------------------------------------------------------


def test_lists_and_counts_load_on_the_worker(env):
    env.tasks.create(TaskDraft("Inbox task", priority=Priority.P1))
    results, counts = [], []
    env.tasks.load(TaskQuery(ListKind.INBOX, TODAY), results.append).result(timeout=5)
    env.tasks.load_counts(counts.append).result(timeout=5)
    env.queue.run()
    assert [s.task.title for s in results[0]] == ["Inbox task"]
    assert counts[0].inbox == 1


def test_tracked_time_is_zero_without_entries(env):
    task_id = env.tasks.create(TaskDraft("x"))
    tracked = env.tasks.tracked(task_id)
    assert (tracked.today_s, tracked.total_s) == (0, 0)


# --- Projects ---------------------------------------------------------------------------


def test_project_lifecycle_with_archive_undo(env):
    pid = env.projects.create(
        ProjectDraft("PMS", key_prefix="proj", url_template="https://p/{key}")
    )
    assert env.changed[-1] == Changed(Entity.PROJECT, frozenset({pid}))
    env.projects.update(pid, ProjectDraft("PMS 2", key_prefix="PROJ"))
    assert env.projects.get(pid).name == "PMS 2"
    assert env.projects.project_for_key("PROJ-9").id == pid
    env.projects.archive(pid)
    assert env.projects.archived()[0].id == pid
    assert env.toasts[-1].message == "Archived “PMS 2”"
    env.undo_last()
    assert env.projects.archived() == []
    assert env.projects.can_delete(pid)
    assert not env.projects.can_delete(1)
    env.projects.delete(pid)
    assert pid not in [p.id for p in env.projects.all(include_archived=True)]
    env.undo_last()  # undoing the old archive toast now finds nothing
    assert env.toasts[-1].message == "Couldn't undo: the project no longer exists"


def test_ticket_url_for_a_task(env):
    pid = env.projects.create(
        ProjectDraft("PMS", key_prefix="PROJ", url_template="https://pms/browse/{key}")
    )
    in_area = env.tasks.get(env.tasks.create(TaskDraft("a", ticket_key="PROJ-5")))
    assert env.projects.ticket_url(in_area) == "https://pms/browse/PROJ-5"
    no_key = env.tasks.get(env.tasks.create(TaskDraft("b", project_id=pid)))
    assert env.projects.ticket_url(no_key) == ""
    assert env.projects.open_task_count(pid) == 1
    assert not env.projects.can_delete(pid)


# --- Calendar cache ---------------------------------------------------------------------


def test_calendar_is_rebuilt_after_settings_or_time_off_change(env, conn):
    first = env.calendar.calendar()
    assert env.calendar.calendar() is first
    conn.execute("INSERT INTO time_off (day, part) VALUES ('2026-11-04', 'full')")
    env.bus.publish(Changed(Entity.TIME_OFF))
    second = env.calendar.calendar()
    assert second is not first
    assert not second.is_working(date(2026, 11, 4))
    env.settings.save(replace(env.settings.current, workdays=frozenset({2})))
    assert env.calendar.calendar() is not second
    env.bus.publish(Changed(Entity.TASK))
    assert env.calendar.calendar() is env.calendar.calendar()


# --- Day watcher ------------------------------------------------------------------------


def test_day_watcher_publishes_on_local_midnight_and_timezone_change():
    clock = ManualClock(datetime(2026, 11, 3, 22, 50, tzinfo=UTC), tz=BERLIN)  # 23:50 local
    bus = EventBus(Queue())
    seen = []
    bus.subscribe(DayChanged, seen.append)
    watcher = DayWatcher(clock, bus)
    assert watcher.today == TODAY
    assert not watcher.check()
    clock.advance(timedelta(minutes=15))
    assert watcher.check()
    assert seen == [DayChanged(date(2026, 11, 4))]
    clock.set_system_tz(ZoneInfo("America/New_York"))
    assert watcher.check()  # same date there, but days must be re-bucketed
    assert seen[-1] == DayChanged(date(2026, 11, 3))
    watcher.stop()  # never started: harmless


def test_day_watcher_tick_keeps_running_after_an_error(monkeypatch):
    watcher = DayWatcher(ManualClock(START, tz=BERLIN), EventBus(Queue()))
    reported = []
    monkeypatch.setattr(day, "report_error", reported.append)
    monkeypatch.setattr(watcher, "check", lambda: 1 / 0)
    assert watcher._on_tick() == GLib.SOURCE_CONTINUE
    assert isinstance(reported[0], ZeroDivisionError)
    watcher.start()
    watcher.start()
    watcher.stop()


def test_short_titles():
    assert short("  Fix   login  ") == "Fix login"
    assert short("x" * 50) == "x" * 39 + "…"
    assert len(short("word " * 20)) <= 40


# --- Undo safety and status changes (code review) ----------------------------------------


def test_undo_never_overwrites_an_edit_made_after_the_action(env):
    task_id = env.tasks.create(TaskDraft("Report"))
    env.tasks.complete(task_id)
    complete_toast = env.toasts[-1]
    env.tasks.update(replace(env.tasks.get(task_id), notes="written after completing"))
    complete_toast.undo()
    task = env.tasks.get(task_id)
    assert (task.status, task.notes) == (TaskStatus.DONE, "written after completing")
    assert env.toasts[-1].message == "Couldn't undo: “Report” was changed since"


def test_resume_has_undo_that_brings_the_hold_back(env):
    task_id = env.tasks.create(TaskDraft("Deploy"))
    env.tasks.put_on_hold(task_id, HoldRequest(HoldReason.APPROVAL, "QA sign-off", TODAY))
    held = env.tasks.get(task_id).hold
    env.tasks.resume(task_id)
    assert env.toasts[-1].message == "Resumed “Deploy”"
    env.undo_last()
    task = env.tasks.get(task_id)
    assert (task.status, task.hold) == (TaskStatus.ON_HOLD, held)


def test_reopen_has_undo_that_keeps_the_completion_time(env):
    task_id = env.tasks.create(TaskDraft("Review"))
    env.tasks.complete(task_id)
    completed_at = env.tasks.get(task_id).completed_at
    env.clock.advance(timedelta(hours=1))
    env.tasks.set_status(task_id, TaskStatus.IN_PROGRESS)
    assert env.toasts[-1].message == "Reopened “Review”"
    env.undo_last()
    task = env.tasks.get(task_id)
    assert (task.status, task.completed_at) == (TaskStatus.DONE, completed_at)


def test_switching_between_to_do_and_in_progress_loses_nothing_and_needs_no_toast(env):
    task_id = env.tasks.create(TaskDraft("x"))
    count = len(env.toasts)
    env.tasks.set_status(task_id, TaskStatus.IN_PROGRESS)
    env.tasks.set_status(task_id, TaskStatus.TO_DO)
    assert len(env.toasts) == count


def test_create_refuses_planning_on_a_non_working_day(env):
    saturday = date(2026, 11, 7)
    with pytest.raises(ValidationError) as info:
        env.tasks.create(TaskDraft("x", plan=PlanRequest(saturday, 30)))
    assert info.value.field == "plan"
    assert "Mon 9 Nov 2026" in info.value.message
    assert env.tasks.default_plan_day() == TODAY
    env.clock.advance(timedelta(days=4))  # Saturday
    assert env.tasks.default_plan_day() == date(2026, 11, 9)
    env.tasks.create(TaskDraft("y", plan=PlanRequest(env.tasks.default_plan_day(), 30)))
