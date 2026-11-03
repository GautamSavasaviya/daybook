"""Timer state machine, recovery (mandatory, SPEC), manual entries and meeting trimming."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import timer, tracking
from daybook.core.errors import NotFoundError, OverlapError, ValidationError
from daybook.core.models import (
    EntrySource,
    Hold,
    HoldReason,
    MeetingDraft,
    PauseReason,
    TaskDraft,
    TaskStatus,
    TimeEntry,
    TimerRun,
    TimerState,
    occurrence_key,
)
from daybook.core.repo import meetings, tasks, time_entries
from daybook.core.repo import timer as timer_repo
from daybook.core.timer import RecoveryChoice

BERLIN = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def new(conn, title="Task", **extra):
    return tasks.create(conn, TaskDraft(title=title, **extra), T0)


def entry(entry_id, task_id, start, end, source=EntrySource.TIMER):
    return TimeEntry(entry_id, task_id, at(start), at(end), "", source)


def stored(conn, task_id=None):
    entries = time_entries.between(conn, at(-24 * 60), at(24 * 60))
    return [
        (e.task_id, e.start, e.end, e.source)
        for e in entries
        if task_id is None or e.task_id == task_id
    ]


# --- Pure state machine ---------------------------------------------------------------------


def test_start_pause_resume_stop_close_segments():
    started = timer.start(None, 7, at(0))
    assert started.closed is None
    assert started.state == TimerState(7, TimerRun.RUNNING, at(0), at(0))

    paused = timer.pause(started.state, at(25))
    assert paused.closed == timer.Closed(7, at(0), at(25))
    assert paused.state is not None
    assert paused.state.run == TimerRun.PAUSED
    assert paused.state.segment_start is None
    assert paused.state.pause_reason == PauseReason.USER

    # Pausing a paused timer, or resuming a running one, changes nothing.
    assert timer.pause(paused.state, at(26)) == timer.Transition(None, paused.state)
    resumed = timer.resume(paused.state, at(30))
    assert resumed.state == TimerState(7, TimerRun.RUNNING, at(30), at(30))
    assert timer.resume(resumed.state, at(31)) == timer.Transition(None, resumed.state)

    stopped = timer.stop(resumed.state, at(40))
    assert stopped == timer.Transition(timer.Closed(7, at(30), at(40)), None)
    assert timer.stop(None, at(41)) == timer.Transition(None, None)
    assert timer.resume(None, at(41)) == timer.Transition(None, None)


def test_starting_another_task_closes_the_current_one():
    running = timer.start(None, 1, at(0)).state
    switched = timer.start(running, 2, at(10))
    assert switched.closed == timer.Closed(1, at(0), at(10))
    assert switched.state is not None
    assert switched.state.task_id == 2
    # Starting the task that is already running only refreshes the heartbeat.
    same = timer.start(switched.state, 2, at(12))
    assert same.closed is None
    assert same.state == TimerState(2, TimerRun.RUNNING, at(10), at(12))


def test_segments_under_a_second_and_backwards_clocks_are_dropped(caplog):
    running = timer.start(None, 1, at(0)).state
    assert timer.close(running, at(0) + timedelta(milliseconds=900)) is None
    with caplog.at_level("WARNING"):
        assert timer.close(running, at(-5)) is None
    assert "ends before it starts" in caplog.text
    assert timer.close(running, at(0) + timedelta(seconds=1)) is not None


def test_elapsed_counts_only_the_live_segment():
    running = timer.start(None, 1, at(0)).state
    assert timer.elapsed(running, at(5)) == timedelta(minutes=5)
    assert timer.elapsed(running, at(-1)) == timedelta()
    assert timer.elapsed(timer.pause(running, at(5)).state, at(9)) == timedelta()
    assert timer.elapsed(None, at(9)) == timedelta()


# --- Recovery (mandatory) -------------------------------------------------------------------


def test_recovery_is_offered_only_for_a_running_timer():
    running = timer.heartbeat(timer.start(None, 3, at(0)).state, at(20))
    assert timer.recovery(running) == timer.Recovery(3, at(0), at(20))
    assert timer.recovery(timer.pause(running, at(21)).state) is None
    assert timer.recovery(None) is None


@pytest.mark.parametrize(
    ("choice", "closed", "state"),
    [
        (RecoveryChoice.KEEP, None, TimerState(3, TimerRun.RUNNING, at(0), at(90))),
        (RecoveryChoice.STOP_AT_LAST_SEEN, timer.Closed(3, at(0), at(20)), None),
        (RecoveryChoice.DISCARD, None, None),
    ],
)
def test_recovery_choices(choice, closed, state):
    running = timer.heartbeat(timer.start(None, 3, at(0)).state, at(20))
    assert timer.recover(running, choice, at(90)) == timer.Transition(closed, state)


def test_recovery_after_a_crash_through_the_repository(conn):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(0))
    timer_repo.heartbeat(conn, at(17))
    # The app dies here. On the next start the running row is still there.
    found = timer.recovery(timer_repo.get(conn))
    assert found == timer.Recovery(task_id, at(0), at(17))

    timer_repo.recover(conn, RecoveryChoice.STOP_AT_LAST_SEEN, at(120))
    assert timer_repo.get(conn) is None
    assert stored(conn) == [(task_id, at(0), at(17), EntrySource.TIMER)]


def test_recovery_keep_and_discard_through_the_repository(conn):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(0))
    timer_repo.recover(conn, RecoveryChoice.KEEP, at(60))
    state = timer_repo.get(conn)
    assert state is not None
    assert (state.segment_start, state.last_seen) == (at(0), at(60))

    timer_repo.recover(conn, RecoveryChoice.DISCARD, at(61))
    assert timer_repo.get(conn) is None
    assert stored(conn) == []
    # Nothing to recover is a no-op.
    timer_repo.recover(conn, RecoveryChoice.DISCARD, at(62))


def test_heartbeat_only_touches_a_running_timer(conn):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(0))
    timer_repo.pause(conn, at(5))
    timer_repo.heartbeat(conn, at(9))
    state = timer_repo.get(conn)
    assert state is not None
    assert state.last_seen == at(5)


# --- Repository transitions -----------------------------------------------------------------


def test_repository_start_switch_pause_resume_stop(conn):
    a, b = new(conn, "a"), new(conn, "b")
    assert timer_repo.start(conn, a, at(0)) is None
    before = timer_repo.start(conn, b, at(10))
    assert before is not None
    assert before.task_id == a
    timer_repo.pause(conn, at(30))
    timer_repo.resume(conn, at(40))
    timer_repo.stop(conn, at(45))
    assert stored(conn) == [
        (a, at(0), at(10), EntrySource.TIMER),
        (b, at(10), at(30), EntrySource.TIMER),
        (b, at(40), at(45), EntrySource.TIMER),
    ]
    assert timer_repo.get(conn) is None
    # Resuming with no timer does nothing.
    timer_repo.resume(conn, at(50))
    assert timer_repo.get(conn) is None


def test_starting_sets_in_progress_and_resumes_on_hold(conn):
    todo = new(conn, "todo")
    held = new(conn, "held")
    tasks.put_on_hold(conn, held, Hold(HoldReason.APPROVAL, "x", T0, None), TODAY, T0)
    timer_repo.start(conn, todo, at(0))
    assert tasks.get(conn, todo).status == TaskStatus.IN_PROGRESS
    timer_repo.start(conn, held, at(5))
    resumed = tasks.get(conn, held)
    assert resumed.status == TaskStatus.IN_PROGRESS
    assert resumed.hold is None


def test_done_and_deleted_tasks_cannot_be_timed(conn):
    done = new(conn, "done")
    tasks.complete(conn, done, T0, TODAY)
    with pytest.raises(ValidationError):
        timer_repo.start(conn, done, at(0))
    gone = new(conn, "gone")
    tasks.soft_delete(conn, gone, T0)
    with pytest.raises(NotFoundError):
        timer_repo.start(conn, gone, at(0))


def test_resume_refuses_a_task_completed_meanwhile(conn):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(0))
    timer_repo.pause(conn, at(5))
    conn.execute(
        "UPDATE task SET status = 'done', completed_at = updated_at WHERE id = ?", (task_id,)
    )
    with pytest.raises(ValidationError):
        timer_repo.resume(conn, at(6))


@pytest.mark.parametrize("action", ["complete", "hold", "delete"])
def test_hold_done_and_delete_stop_the_timer_and_keep_the_time(conn, action):
    task_id = new(conn)
    other = new(conn, "other")
    timer_repo.start(conn, task_id, at(0))
    match action:
        case "complete":
            tasks.complete(conn, task_id, at(20), TODAY)
        case "hold":
            tasks.put_on_hold(
                conn, task_id, Hold(HoldReason.BLOCKED, None, at(20), None), TODAY, at(20)
            )
        case "delete":
            tasks.soft_delete(conn, task_id, at(20))
    assert timer_repo.get(conn) is None
    assert stored(conn) == [(task_id, at(0), at(20), EntrySource.TIMER)]
    # A timer on another task is left alone.
    timer_repo.start(conn, other, at(30))
    assert not timer_repo.stop_on(conn, task_id, at(31))
    assert timer_repo.get(conn) is not None


def test_a_closed_segment_overlapping_logged_time_is_dropped_not_fatal(conn, caplog):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(0))
    # Only a clock jump could do this; force it past the checks.
    time_entries.insert(conn, task_id, at(5), at(10), source=EntrySource.MANUAL)
    with caplog.at_level("WARNING"):
        timer_repo.stop(conn, at(20))
    assert timer_repo.get(conn) is None
    assert "overlapping" in caplog.text


# --- Manual entries -------------------------------------------------------------------------


def test_check_entry_rules():
    others = [entry(1, 5, 0, 30, EntrySource.MANUAL)]
    with pytest.raises(ValidationError) as caught:
        tracking.check_entry(at(40), at(40), at(60), others, None)
    assert caught.value.field == "end"
    with pytest.raises(ValidationError, match="up to now"):
        tracking.check_entry(at(40), at(61), at(60), others, None)
    with pytest.raises(ValidationError, match="16 hours"):
        tracking.check_entry(at(-17 * 60), at(40), at(60), [], None)
    with pytest.raises(OverlapError) as overlap:
        tracking.check_entry(at(20), at(40), at(60), others, None)
    assert (overlap.value.entry_id, overlap.value.task_id) == (1, 5)
    # Touching is not overlapping (half-open), and an entry may overlap its old self.
    tracking.check_entry(at(30), at(40), at(60), others, None)
    tracking.check_entry(at(10), at(20), at(60), others, None, editing=1)


def test_check_entry_refuses_the_live_segment():
    running = timer.start(None, 9, at(50)).state
    with pytest.raises(OverlapError) as caught:
        tracking.check_entry(at(40), at(55), at(60), [], running)
    assert caught.value.entry_id is None
    assert caught.value.task_id == 9
    tracking.check_entry(at(40), at(50), at(60), [], running)


def test_to_interval_handles_midnight_and_dst():
    typed = tracking.EntryInput(TODAY, time(9, 0), time(10, 30))
    assert tracking.to_interval(typed, BERLIN) == (
        datetime(2026, 11, 3, 8, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 9, 30, tzinfo=UTC),
    )
    late = tracking.EntryInput(TODAY, time(23, 0), time(0, 0))
    assert tracking.to_interval(late, BERLIN)[1] == datetime(2026, 11, 3, 23, 0, tzinfo=UTC)


def test_manual_entries_add_edit_delete_restore(conn):
    task_id = new(conn)
    first = time_entries.add_manual(conn, task_id, at(0), at(30), note=" call ", now=at(60))
    assert time_entries.get(conn, first).note == "call"
    with pytest.raises(OverlapError):
        time_entries.add_manual(conn, task_id, at(20), at(40), note="", now=at(60))
    second = time_entries.add_manual(conn, task_id, at(30), at(45), note="", now=at(60))

    before = time_entries.update(conn, second, at(35), at(50), note="later", now=at(60))
    assert before.start == at(30)
    changed = time_entries.get(conn, second)
    assert (changed.start, changed.end, changed.note) == (at(35), at(50), "later")
    with pytest.raises(OverlapError):
        time_entries.update(conn, second, at(25), at(50), note="", now=at(60))

    removed = time_entries.delete(conn, first)
    assert [e.id for e in time_entries.for_task(conn, task_id)] == [second]
    again = time_entries.reinsert(conn, removed)
    assert replace(time_entries.get(conn, again), id=removed.id) == removed
    with pytest.raises(NotFoundError):
        time_entries.get(conn, 999)
    with pytest.raises(NotFoundError):
        time_entries.add_manual(conn, 999, at(70), at(80), note="", now=at(90))


def test_editing_the_times_of_a_timer_entry_makes_it_manual(conn):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(0))
    timer_repo.stop(conn, at(30))
    (logged,) = time_entries.for_task(conn, task_id)
    time_entries.update(conn, logged.id, at(0), at(30), note="only the note", now=at(60))
    assert time_entries.get(conn, logged.id).source == EntrySource.TIMER
    time_entries.update(conn, logged.id, at(0), at(25), note="", now=at(60))
    assert time_entries.get(conn, logged.id).source == EntrySource.MANUAL


def test_the_trigger_is_the_backstop(conn):
    task_id = new(conn)
    time_entries.insert(conn, task_id, at(0), at(30), source=EntrySource.MANUAL)
    with pytest.raises(ValidationError):
        time_entries.insert(conn, task_id, at(10), at(20), source=EntrySource.MANUAL)


def test_reinsert_refuses_a_purged_task(conn):
    task_id = new(conn)
    entry_id = time_entries.add_manual(conn, task_id, at(0), at(30), note="", now=at(60))
    removed = time_entries.delete(conn, entry_id)
    conn.execute("DELETE FROM task WHERE id = ?", (task_id,))
    with pytest.raises(NotFoundError):
        time_entries.reinsert(conn, removed)


def test_entries_by_day_and_runs():
    lo, hi = at(-60), at(600)
    entries = [
        entry(1, 1, 0, 20),
        entry(2, 1, 25, 40),  # a pause on the same task merges
        entry(3, 2, 40, 50),
        entry(4, 1, 50, 55),  # back to task 1 after task 2: a new block
    ]
    running = timer.start(None, 1, at(55)).state
    result = tracking.runs(entries, running, lo, hi, at(70))
    assert [(r.task_id, r.start, r.end, r.seconds // 60, r.live) for r in result] == [
        (1, at(0), at(40), 35, False),
        (2, at(40), at(50), 10, False),
        (1, at(50), at(70), 20, True),
    ]
    # Clipping to the day.
    clipped = tracking.runs(entries, None, at(10), at(30), at(70))
    assert [(r.start, r.end, r.seconds // 60) for r in clipped] == [(at(10), at(30), 15)]


def test_on_day_and_ending_after(conn):
    task_id = new(conn)
    time_entries.insert(conn, task_id, at(0), at(30), source=EntrySource.TIMER)
    time_entries.insert(conn, task_id, at(-24 * 60), at(-23 * 60), source=EntrySource.TIMER)
    assert [e.start for e in time_entries.on_day(conn, TODAY, BERLIN)] == [at(0)]
    assert [e.start for e in time_entries.ending_after(conn, at(10))] == [at(0)]


# --- Meeting trimming -----------------------------------------------------------------------


def test_trim_for_a_meeting_that_started_earlier():
    entries = [
        entry(1, 1, -30, -10),  # before the cut: untouched
        entry(2, 1, -10, 5),  # crosses the cut: shortened
        entry(3, 2, 5, 10),  # after the cut: deleted
    ]
    running = timer.start(None, 2, at(10)).state
    trim = timer.trim_for_meeting(entries, running, at(0), at(12), "5:key")
    assert [e.id for e in trim.shorten] == [2]
    assert [e.id for e in trim.delete] == [3]
    assert trim.removed_s == {1: 5 * 60, 2: 7 * 60}
    assert trim.removed_total_s == 12 * 60
    # The live segment started after the cut, so none of it is kept; the timer pauses.
    assert trim.transition.closed is None
    assert trim.transition.state == TimerState(
        2, TimerRun.PAUSED, None, at(12), PauseReason.MEETING, "5:key"
    )


def test_trim_keeps_live_time_before_the_cut_and_a_paused_timer_stays_paused():
    running = timer.start(None, 1, at(-20)).state
    trim = timer.trim_for_meeting([], running, at(-5), at(0), "k")
    assert trim.transition.closed == timer.Closed(1, at(-20), at(-5))
    assert trim.removed_s == {1: 5 * 60}
    # Starting a meeting now just pauses.
    now_trim = timer.trim_for_meeting([], running, at(0), at(0), "k")
    assert now_trim.transition.closed == timer.Closed(1, at(-20), at(0))
    assert now_trim.removed_s == {}
    paused = timer.pause(running, at(-1)).state
    assert timer.trim_for_meeting([], paused, at(-5), at(0)).transition.state == paused
    assert timer.trim_for_meeting([], None, at(-5), at(0)).transition.state is None


def test_trim_refuses_manual_time_after_the_start():
    manual = [entry(1, 1, -10, -2, EntrySource.MANUAL)]
    with pytest.raises(ValidationError) as caught:
        timer.trim_for_meeting(manual, None, at(-5), at(0))
    assert caught.value.field == "started"
    # Manual time before the start is fine.
    assert timer.trim_for_meeting(manual, None, at(-2), at(0)).removed_s == {}


def test_starting_an_adhoc_meeting_trims_in_one_transaction(conn):
    task_id = new(conn)
    timer_repo.start(conn, task_id, at(-20))
    draft = MeetingDraft(title="Call", start=at(-12), duration_min=30, reminder_offsets=())
    assert timer_repo.plan_trim(conn, at(-12), at(0)).removed_s == {task_id: 12 * 60}
    meeting_id, trim = meetings.start_adhoc(conn, draft, "Europe/Berlin", at(0), open_ended=True)
    assert trim.removed_s == {task_id: 12 * 60}
    assert stored(conn) == [(task_id, at(-20), at(-12), EntrySource.TIMER)]
    state = timer_repo.get(conn)
    assert state is not None
    assert state.pause_reason == PauseReason.MEETING
    assert state.paused_by_meeting_key == occurrence_key(meeting_id, at(-12))


def test_a_refused_adhoc_start_writes_nothing(conn):
    task_id = new(conn)
    time_entries.add_manual(conn, task_id, at(-10), at(-1), note="", now=at(0))
    draft = MeetingDraft(title="Call", start=at(-15), duration_min=30, reminder_offsets=())
    with pytest.raises(ValidationError):
        meetings.start_adhoc(conn, draft, "Europe/Berlin", at(0), open_ended=False)
    assert meetings.active(conn) == []


def test_trim_shortens_and_deletes_stored_entries(conn):
    task_id = new(conn)
    time_entries.insert(conn, task_id, at(-30), at(-10), source=EntrySource.TIMER)
    time_entries.insert(conn, task_id, at(-8), at(-2), source=EntrySource.TIMER)
    timer_repo.apply_trim(conn, at(-15), at(0), "m")
    assert stored(conn) == [(task_id, at(-30), at(-15), EntrySource.TIMER)]
