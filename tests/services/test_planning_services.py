import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core.clock import ManualClock
from daybook.core.errors import ValidationError
from daybook.core.hold import HoldRequest
from daybook.core.models import (
    HoldReason,
    MeetingDraft,
    OccurrenceEdit,
    PlanRequest,
    Recurrence,
    RecurrenceFreq,
    TaskDraft,
    TaskStatus,
    TimeOffPart,
)
from daybook.core.repo import app_state
from daybook.core.repo import plan as plan_repo
from daybook.services.calendar import CalendarService
from daybook.services.events import Changed, DayChanged, Entity, EventBus, Toast
from daybook.services.meetings import MeetingService
from daybook.services.plan import PlanService
from daybook.services.rollover import WATERMARK, RolloverService
from daybook.services.settings import SettingsService
from daybook.services.tasks import TaskService
from daybook.services.time_off import TimeOffService
from daybook.services.worker import Worker

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 3, 8, 30, tzinfo=UTC)  # Tuesday 09:30 in Berlin
TODAY = date(2026, 11, 3)
WED = date(2026, 11, 4)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


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
        self.conn = conn
        self.clock = ManualClock(START, tz=BERLIN)
        self.queue = Queue()
        self.bus = EventBus(self.queue)
        self.settings = SettingsService(conn, self.bus)
        self.worker = Worker(db_path, self.queue)
        self.calendar = CalendarService(conn, self.settings, self.bus)
        self.tasks = TaskService(conn, self.bus, self.clock, self.worker, self.calendar)
        self.meetings = MeetingService(conn, self.bus, self.clock, self.settings)
        self.plan = PlanService(
            conn,
            self.bus,
            self.clock,
            worker=self.worker,
            calendar=self.calendar,
            meetings=self.meetings,
            tasks=self.tasks,
        )
        self.time_off = TimeOffService(conn, self.bus, self.clock, self.calendar)
        self.rollover = RolloverService(conn, self.bus, self.clock, self.settings, self.calendar)
        self.toasts: list[Toast] = []
        self.changed: list[Changed] = []
        self.bus.subscribe(Toast, self.toasts.append)
        self.bus.subscribe(Changed, self.changed.append)

    def undo_last(self) -> None:
        undo = self.toasts[-1].undo
        assert undo is not None
        undo()


@pytest.fixture
def env(conn, db_path):
    environment = Env(conn, db_path)
    yield environment
    environment.worker.shutdown()


def standup(**changes) -> MeetingDraft:
    base = {
        "title": "Standup",
        "start": at(date(2026, 11, 2), 10),
        "duration_min": 15,
        "link": "https://meet/s",
        "recurrence": Recurrence(RecurrenceFreq.WEEKDAYS),
    }
    base.update(changes)
    return MeetingDraft(**base)


# --- Meetings ---------------------------------------------------------------------------


def test_meetings_create_occurrences_and_edit_one(env):
    meeting_id = env.meetings.create(standup())
    assert env.meetings.get(meeting_id).tzid == "Europe/Berlin"
    assert env.changed[-1] == Changed(Entity.MEETING)
    [today] = env.meetings.day_occurrences(TODAY)
    assert today.start == at(TODAY, 10)
    env.meetings.edit_occurrence(
        meeting_id,
        today.original_start,
        OccurrenceEdit("Standup (long)", at(TODAY, 11), 45, today.link, today.reminder_offsets),
    )
    [moved] = env.meetings.day_occurrences(TODAY)
    assert (moved.title, moved.start, moved.duration_min, moved.edited) == (
        "Standup (long)",
        at(TODAY, 11),
        45,
        True,
    )
    assert env.meetings.occurrence(meeting_id, today.original_start).start == at(TODAY, 11)
    assert env.meetings.occurrence(meeting_id, at(TODAY, 10, 5)) is None
    assert env.meetings.default_offsets() == (10, 1)
    assert [m.id for m in env.meetings.all()] == [meeting_id]


def test_cancel_one_occurrence_with_undo(env):
    meeting_id = env.meetings.create(standup())
    [today] = env.meetings.day_occurrences(TODAY)
    env.meetings.cancel_occurrence(meeting_id, today.original_start)
    assert env.meetings.day_occurrences(TODAY) == []
    assert env.toasts[-1].message == "Cancelled “Standup” on today"
    env.undo_last()
    assert len(env.meetings.day_occurrences(TODAY)) == 1


def test_series_edit_counts_and_discards_orphaned_changes(env):
    meeting_id = env.meetings.create(standup())
    [today] = env.meetings.day_occurrences(TODAY)
    env.meetings.cancel_occurrence(meeting_id, today.original_start)
    assert env.meetings.discarded_by(meeting_id, standup(title="Daily")) == 0
    later = standup(start=at(date(2026, 11, 2), 11))
    assert env.meetings.discarded_by(meeting_id, later) == 1
    env.meetings.update_series(meeting_id, later)
    [again] = env.meetings.day_occurrences(TODAY)  # the cancellation is gone
    assert again.start == at(TODAY, 11)


def test_delete_series_with_undo_and_purge(env):
    meeting_id = env.meetings.create(standup())
    env.meetings.delete(meeting_id)
    assert env.meetings.day_occurrences(TODAY) == []
    env.undo_last()
    assert len(env.meetings.day_occurrences(TODAY)) == 1
    env.meetings.delete(meeting_id)
    env.clock.advance(timedelta(days=2))
    assert env.meetings.purge_deleted() == 1
    env.toasts[-1].undo()
    assert env.toasts[-1].message == "Couldn't undo: the meeting no longer exists"


def test_meeting_now_open_ended_and_ending_it(env):
    env.clock.advance(timedelta(seconds=20))
    meeting_id = env.meetings.start_now("  ", None, None, started_ago_min=10)
    meeting = env.meetings.get(meeting_id)
    assert (meeting.title, meeting.is_adhoc, meeting.open_ended, meeting.duration_min) == (
        "Meeting",
        True,
        True,
        30,
    )
    assert meeting.start == at(TODAY, 9, 20)
    [running] = env.meetings.open_adhoc()
    assert running.meeting_id == meeting_id
    env.clock.advance(timedelta(minutes=40))  # now 10:10: past the provisional 30 min
    assert env.meetings.open_adhoc()  # still open: the length is unknown
    env.meetings.end_now(meeting_id)
    ended = env.meetings.get(meeting_id)
    assert (ended.duration_min, ended.open_ended) == (50, False)
    assert env.toasts[-1].message == "Ended “Meeting” (50m)"
    assert env.meetings.open_adhoc() == []
    env.undo_last()
    assert env.meetings.get(meeting_id).open_ended
    with pytest.raises(ValidationError):
        env.meetings.start_now("x", None, 30, started_ago_min=31)


def test_meeting_now_with_a_known_length_ends_by_itself(env):
    env.meetings.start_now("Escalation", "https://meet/e", 15)
    assert env.meetings.in_progress()
    env.clock.advance(timedelta(minutes=16))
    assert env.meetings.open_adhoc() == []


# --- Plan -------------------------------------------------------------------------------


def test_day_plan_schedules_around_meetings_from_now(env):
    env.meetings.create(standup())
    a = env.tasks.create(TaskDraft("a", plan=PlanRequest(TODAY, 60)))
    b = env.tasks.create(TaskDraft("b", plan=PlanRequest(TODAY, None)))
    done = env.tasks.create(TaskDraft("done", plan=PlanRequest(TODAY, 30)))
    env.tasks.complete(done)
    day = env.plan.day_plan(TODAY)
    assert [e.summary.task.id for e in day.entries] == [a, b, done]
    assert not day.entries[2].open
    first = day.schedule.task_segments()[0]
    assert (first.task_id, first.start, first.end) == (a, at(TODAY, 9, 30), at(TODAY, 10))
    assert day.schedule.task_segments()[1].start == at(TODAY, 10, 15)  # after the standup
    assert day.schedule.capacity.flexible_count == 1
    assert day.entry(day.entries[0].item.id).summary.task.title == "a"
    assert day.entry(None) is None
    assert day.lunch is not None
    assert day.leave is None


def test_add_allotment_order_and_remove_with_undo(env):
    a, b, c = (env.tasks.create(TaskDraft(t)) for t in "abc")
    for task_id in (a, b, c):
        env.plan.add(task_id, TODAY, 30)
    ids = [e.item.id for e in env.plan.entries(TODAY)]
    env.plan.move_by(ids[2], -1)
    assert [e.summary.task.id for e in env.plan.entries(TODAY)] == [a, c, b]
    env.plan.move_to(ids[0], 5)
    assert [e.summary.task.id for e in env.plan.entries(TODAY)] == [c, b, a]
    env.plan.move_by(ids[2], -5)
    assert env.plan.entries(TODAY)[0].summary.task.id == c
    env.plan.set_allotment(ids[1], None)
    assert env.plan.entries(TODAY)[1].item.allotted_min is None
    env.plan.remove(ids[1])
    assert env.toasts[-1].message == "Removed “b” from the plan"
    env.undo_last()
    assert [e.summary.task.id for e in env.plan.entries(TODAY)] == [c, b, a]
    env.plan.remove(env.plan.entries(TODAY)[1].item.id)
    env.plan.add(b, TODAY, 15)
    env.undo_last()  # planned again meanwhile
    assert env.toasts[-1].message == "Couldn't undo: the plan has changed since"


def test_add_refuses_non_working_days_resumes_follow_ups_and_skips_done(env):
    task_id = env.tasks.create(TaskDraft("x"))
    with pytest.raises(ValidationError):
        env.plan.add(task_id, date(2026, 11, 7), 30)
    with pytest.raises(ValidationError):
        env.plan.add(task_id, TODAY, 0)
    env.tasks.put_on_hold(task_id, HoldRequest(HoldReason.SOMEONE, None, TODAY))
    env.plan.add(task_id, TODAY, 30)
    assert env.tasks.get(task_id).status is TaskStatus.IN_PROGRESS
    done = env.tasks.create(TaskDraft("done"))
    env.tasks.complete(done)
    env.plan.add(done, TODAY, 30)
    assert done not in [e.summary.task.id for e in env.plan.entries(TODAY)]


def test_move_overflow_to_the_next_working_day_with_undo(env):
    big = env.tasks.create(TaskDraft("big", plan=PlanRequest(TODAY, 8 * 60)))
    extra = env.tasks.create(TaskDraft("extra", plan=PlanRequest(TODAY, 60)))
    overflow = env.plan.day_plan(TODAY).schedule.overflow
    assert {o.task_id for o in overflow} == {big, extra}
    assert env.plan.move_overflow(TODAY) == 2
    assert [e.summary.task.id for e in env.plan.entries(WED)] == [big, extra]
    assert env.toasts[-1].message == "Moved 2 tasks to tomorrow"
    env.undo_last()
    assert [e.summary.task.id for e in env.plan.entries(TODAY)] == [big, extra]
    assert env.tasks.get(big).moved_count == 0
    assert env.plan.move_overflow(WED) == 0


def test_candidates_load_and_next_day(env):
    env.tasks.create(TaskDraft("due", due_date=TODAY))
    results = []
    env.plan.load_candidates(TODAY, results.append).result(timeout=5)
    env.queue.run()
    assert [s.task.title for s in results[0]] == ["due"]
    assert env.plan.next_day() == WED
    assert env.plan.is_working(TODAY)
    env.clock.advance(timedelta(days=3))
    assert env.plan.next_day() == date(2026, 11, 9)


# --- Time off ---------------------------------------------------------------------------


def test_mark_full_day_moves_the_plan_and_undo(env):
    a = env.tasks.create(TaskDraft("a", plan=PlanRequest(WED, 30)))
    env.tasks.create(TaskDraft("due while away", due_date=WED))
    assert [s.task.title for s in env.time_off.due_during(WED, WED)] == ["due while away"]
    assert env.time_off.mark(WED, WED, TimeOffPart.FULL, "dentist") == 1
    assert env.toasts[-1].message == "Marked tomorrow off · 1 task moved"
    assert [e.summary.task.id for e in env.plan.entries(date(2026, 11, 5))] == [a]
    assert not env.calendar.calendar().is_working(WED)
    assert [t.day for t in env.time_off.upcoming()] == [WED]
    env.undo_last()
    assert env.calendar.calendar().is_working(WED)
    assert [e.summary.task.id for e in env.plan.entries(WED)] == [a]


def test_mark_a_range_skips_weekends_and_validates(env):
    env.time_off.mark(date(2026, 11, 6), date(2026, 11, 10), TimeOffPart.FULL)
    assert [t.day for t in env.time_off.upcoming()] == [
        date(2026, 11, 6),
        date(2026, 11, 9),
        date(2026, 11, 10),
    ]
    assert env.toasts[-1].message == "Marked Fri to Tue 10 Nov off"
    for args, field in (
        ((TODAY - timedelta(days=1), TODAY, TimeOffPart.FULL), "first_day"),
        ((WED, TODAY, TimeOffPart.FULL), "last_day"),
        ((TODAY, TODAY + timedelta(days=70), TimeOffPart.FULL), "last_day"),
        ((TODAY, WED, TimeOffPart.AM), "part"),
        ((date(2026, 11, 7), date(2026, 11, 8), TimeOffPart.FULL), "first_day"),
    ):
        with pytest.raises(ValidationError) as info:
            env.time_off.mark(*args)
        assert info.value.field == field


def test_half_day_and_remove_with_undo(env):
    env.time_off.mark(TODAY, TODAY, TimeOffPart.PM)
    assert env.calendar.calendar().day_window(TODAY, BERLIN).end == at(TODAY, 13)
    env.time_off.remove(TODAY)
    assert env.toasts[-1].message == "today is a working day again"
    assert env.calendar.calendar().time_off(TODAY) is None
    env.undo_last()
    assert env.calendar.calendar().time_off(TODAY) is TimeOffPart.PM
    env.time_off.remove(WED)  # nothing there: nothing happens


# --- Rollover ---------------------------------------------------------------------------


def test_rollover_runs_once_per_working_day(env, conn):
    yesterday = date(2026, 11, 2)
    a = env.tasks.create(TaskDraft("a"))
    plan_repo.add(conn, a, yesterday, 60)
    assert env.rollover.run() == 1
    [item] = env.plan.entries(TODAY)
    assert (item.item.allotted_min, item.item.carry_count, item.item.carried_from) == (
        60,
        1,
        yesterday,
    )
    assert env.rollover.run() == 0  # idempotent
    assert app_state.get(conn, WATERMARK) == "2026-11-03"


def test_rollover_waits_for_a_working_day_and_follows_day_changes(env, conn):
    env.clock.advance(timedelta(days=4))  # Saturday 7 Nov
    a = env.tasks.create(TaskDraft("a"))
    plan_repo.add(conn, a, date(2026, 11, 6), 30)
    assert env.rollover.run() == 0
    env.clock.advance(timedelta(days=2))  # Monday
    env.bus.publish(DayChanged(date(2026, 11, 9)))
    [item] = env.plan.entries(date(2026, 11, 9))
    assert item.item.carried_from == date(2026, 11, 6)


def test_rollover_off_advances_the_watermark_without_carrying(env, conn):
    env.settings.save(replace(env.settings.current, auto_carry_over=False))
    a = env.tasks.create(TaskDraft("a"))
    plan_repo.add(conn, a, date(2026, 11, 2), 30)
    assert env.rollover.run() == 0
    assert env.plan.entries(TODAY) == []
    assert app_state.get(conn, WATERMARK) == "2026-11-03"


def test_rollover_ignores_a_damaged_watermark(env, conn):
    app_state.put(conn, WATERMARK, "garbage")
    a = env.tasks.create(TaskDraft("a"))
    plan_repo.add(conn, a, date(2026, 11, 2), 30)
    assert env.rollover.run() == 1


# --- Recurring tasks --------------------------------------------------------------------


def test_completing_a_repeating_task_toasts_the_next_due_date_and_undo_removes_it(env):
    task_id = env.tasks.create(
        TaskDraft("Report", due_date=TODAY, recurrence=Recurrence(RecurrenceFreq.WEEKLY))
    )
    env.tasks.complete(task_id)
    assert env.toasts[-1].message == "Completed “Report”; next one due Tue 10 Nov"
    next_id = env.tasks.get(task_id).id + 1
    assert env.tasks.get(next_id).recurs_from_id == task_id
    env.undo_last()
    assert env.tasks.get(task_id).status is TaskStatus.TO_DO
    with pytest.raises(Exception, match="not found"):
        env.tasks.get(next_id)
