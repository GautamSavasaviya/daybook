from dataclasses import replace
from datetime import date, time, timedelta

import pytest

from daybook.core.hold import HoldRequest
from daybook.core.models import HoldReason, MeetingDraft, TaskStatus
from daybook.core.reschedule import MoveChoice, MoveKind
from daybook.core.tracking import EntryInput
from daybook.services.reports import ReportService
from tests.services.test_planning_services import TODAY, at
from tests.services.test_timer_service import TimerEnv

MON = date(2026, 11, 2)


class ReportEnv(TimerEnv):
    def __init__(self, conn, db_path) -> None:
        super().__init__(conn, db_path)
        self.reports = ReportService(
            conn,
            self.clock,
            settings=self.settings,
            calendar=self.calendar,
            plan=self.plan,
            worker=self.worker,
        )


@pytest.fixture
def env(conn, db_path):
    environment = ReportEnv(conn, db_path)
    yield environment
    environment.worker.shutdown()


def log(env, task_id, day, start, end):
    env.timer.add_entry(task_id, EntryInput(day, start, end))


def test_timesheet_includes_the_running_timer_and_follows_the_rounding_setting(env):
    task_id = env.new("Fix login", ticket_key="PROJ-1")
    log(env, task_id, TODAY, time(8, 0), time(8, 7))  # before the clock's 09:30
    env.timer.start(task_id)
    env.clock.advance(timedelta(minutes=20))
    sheet = env.reports.timesheet((TODAY,))
    (row,) = sheet.rows
    assert row.seconds[TODAY] == (7 + 20) * 60
    assert row.minutes[TODAY] == 30  # 27 min, to the nearest 15
    env.settings.save(replace(env.settings.current, timesheet_rounding_min=0))
    assert env.reports.timesheet((TODAY,)).rows[0].minutes[TODAY] == 27


def test_standup_content_for_yesterday_today_and_blockers(env):
    done = env.new("Fix login", ticket_key="PROJ-1", requested_by="Rahul")
    other = env.new("API tests")
    log(env, done, MON, time(9, 0), time(10, 30))
    log(env, other, MON, time(11, 0), time(11, 40))
    env.conn.execute(
        "UPDATE task SET status = 'done', completed_at = ? WHERE id = ?",
        ("2026-11-02T14:00:00Z", done),
    )
    held = env.new("Deploy", ticket_key="PROJ-9")
    env.tasks.put_on_hold(held, HoldRequest(HoldReason.APPROVAL, "QA sign-off"))
    env.plan.add(other, TODAY, 60)
    result = env.reports.standup()
    assert result.yesterday == (
        "PROJ-1 Fix login (for Rahul) — done, 1h 30m",
        "API tests — 40m",
    )
    assert result.plan == ("API tests — 1h 00m",)
    assert result.blockers == (
        "PROJ-9 Deploy — waiting for approval or review, since today (QA sign-off)",
    )


def test_weekly_report_runs_on_the_worker(env):
    task_id = env.new("Fix login", ticket_key="PROJ-1")
    env.plan.add(task_id, TODAY, 60)
    log(env, task_id, MON, time(9, 0), time(10, 30))
    env.meetings.create(MeetingDraft("Standup", at(TODAY, 10), 15, None, reminder_offsets=()))
    got = []
    future = env.reports.weekly(env.reports.week_of(TODAY), got.append)
    future.result(timeout=10)
    env.queue.run()
    (report,) = got
    assert report.focus_min == 90
    assert report.tasks[0].planned_min == 60
    assert report.tasks[0].actual_min == 90
    assert report.meeting_min == 0  # Tuesday 10:00 hasn't happened at 09:30


def test_move_many_has_one_toast_and_one_undo(env):
    a, b, held = env.new("a"), env.new("b"), env.new("held")
    env.plan.add(a, TODAY, 30)
    env.plan.add(b, TODAY, 30)
    env.tasks.put_on_hold(held, HoldRequest(HoldReason.OTHER, ""))
    env.toasts.clear()
    moved = env.tasks.move_many([a, b, held, a], MoveChoice(MoveKind.TOMORROW))
    assert moved == 2
    assert [t.message for t in env.toasts] == ["Moved 2 tasks to tomorrow"]
    assert env.tasks.get(a).moved_count == env.tasks.get(b).moved_count == 1
    assert env.plan.entries(TODAY) == []
    env.undo_last()
    assert {e.summary.task.id for e in env.plan.entries(TODAY)} == {a, b}
    assert env.tasks.get(a).moved_count == 0

    env.tasks.move_many([a], MoveChoice(MoveKind.UNPLANNED))
    assert env.toasts[-1].message == "1 task is unplanned"
    assert env.tasks.plan_items(a) == []
    assert env.tasks.move_many([held], MoveChoice(MoveKind.TOMORROW)) == 0
    assert env.tasks.get(held).status == TaskStatus.ON_HOLD
