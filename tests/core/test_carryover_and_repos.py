from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import carryover
from daybook.core.carryover import CarriedItem, SourceItem
from daybook.core.errors import ChangedSinceError, NotFoundError, ValidationError
from daybook.core.models import (
    Hold,
    HoldReason,
    MeetingDraft,
    OccurrenceEdit,
    PlanRequest,
    Priority,
    Recurrence,
    RecurrenceFreq,
    TaskDraft,
    TaskStatus,
    TimeOff,
    TimeOffPart,
)
from daybook.core.repo import app_state, leave, meetings, plan, tasks, time_entries, time_off
from daybook.core.tasklists import CandidateReason, ListKind, TaskQuery, candidate_reason

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)  # Tuesday
MON = date(2026, 11, 2)
FRI = date(2026, 10, 30)


def source(task_id, day, allotted=60, tracked=0, status=TaskStatus.TO_DO, **extra):
    base = {
        "plan_date": day,
        "position": 0,
        "task_id": task_id,
        "allotted_min": allotted,
        "carry_count": 0,
        "tracked_min": tracked,
        "status": status,
        "deleted": False,
    }
    base.update(extra)
    return SourceItem(**base)


# --- Carry-over (PLAN §5.2 mandatory cases) ----------------------------------------------


def test_normal_next_day_carries_the_remaining_time():
    [item] = carryover.rollover([source(1, MON, 60, tracked=20)], set(), TODAY)
    assert item == CarriedItem(1, 45, 1, MON)  # 40 min left, rounded up to 45


def test_weekend_and_leave_gaps_and_five_days_closed_carry_from_the_latest_day():
    sources = [source(1, FRI - timedelta(days=4), 30), source(1, FRI, 90, carry_count=2)]
    [item] = carryover.rollover(sources, set(), TODAY)
    assert (item.allotted_min, item.carry_count, item.carried_from) == (90, 3, FRI)


def test_partly_and_fully_tracked_items():
    assert carryover.carried_minutes(60, 50) == 15
    assert carryover.carried_minutes(60, 60) == 15  # fully tracked but not done
    assert carryover.carried_minutes(60, 90) == 15
    assert carryover.carried_minutes(100, 0) == 105


def test_rest_of_day_stays_rest_of_day():
    [item] = carryover.rollover([source(1, MON, None, tracked=300)], set(), TODAY)
    assert item.allotted_min is None


@pytest.mark.parametrize(
    "changes",
    [{"status": TaskStatus.DONE}, {"status": TaskStatus.ON_HOLD}, {"deleted": True}],
)
def test_done_on_hold_and_deleted_tasks_are_not_carried(changes):
    assert carryover.rollover([source(1, MON, **changes)], set(), TODAY) == []


def test_tasks_already_planned_today_and_todays_own_items_are_left_alone():
    sources = [source(1, MON), source(2, MON), source(3, TODAY)]
    assert [i.task_id for i in carryover.rollover(sources, {1}, TODAY)] == [2]


def test_original_order_by_day_then_position():
    sources = [source(1, MON, position=1), source(2, MON, position=0), source(3, FRI, position=5)]
    assert [i.task_id for i in carryover.rollover(sources, set(), TODAY)] == [3, 2, 1]


# --- Repository: carry-over sources, rollover and tracked time ----------------------------


def new(conn, title="Task", **fields) -> int:
    return tasks.create(conn, TaskDraft(title, **fields), NOW)


def test_plan_sources_and_apply_rollover_are_idempotent(conn):
    a, b = new(conn, "a"), new(conn, "b")
    plan.add(conn, a, MON, 60)
    plan.add(conn, b, FRI, None)
    plan.add(conn, b, TODAY, 30)
    rows = plan.since(conn, FRI - timedelta(days=1), TODAY)
    assert sorted((r["plan_date"], r["task_id"]) for r in rows) == [
        ("2026-10-30", b),
        ("2026-11-02", a),
    ]
    assert len(plan.since(conn, None, TODAY)) == 2
    plan.apply_rollover(conn, TODAY, [CarriedItem(a, 45, 1, MON)], "wm")
    items = plan.items_on(conn, TODAY)
    assert [
        (i.task_id, i.allotted_min, i.carry_count, i.carried_from, i.position) for i in items
    ] == [
        (b, 30, 0, None, 0),
        (a, 45, 1, MON, 1),
    ]
    assert app_state.get(conn, "wm") == "2026-11-03"


def test_tracked_on_day_per_task(conn):
    a, b = new(conn, "a"), new(conn, "b")
    conn.execute(
        "INSERT INTO time_entry (task_id, start_utc, end_utc, source) VALUES "
        "(?, '2026-11-02T22:30:00Z', '2026-11-02T23:30:00Z', 'manual'), "
        "(?, '2026-11-03T08:00:00Z', '2026-11-03T08:20:00Z', 'manual')",
        (a, b),
    )
    conn.execute(
        "INSERT INTO timer_state (id, task_id, state, segment_start_utc, last_seen_utc, "
        "updated_at) VALUES (1, ?, 'running', '2026-11-03T08:50:00Z', "
        "'2026-11-03T08:50:00Z', '2026-11-03T08:50:00Z')",
        (b,),
    )
    assert time_entries.tracked_on_day(conn, [a, b], TODAY, BERLIN, NOW) == {a: 30, b: 30}
    assert time_entries.tracked_on_day(conn, [], TODAY, BERLIN, NOW) == {}


# --- Repository: plan edits ---------------------------------------------------------------


def test_allotment_remove_reinsert_reorder_and_move(conn):
    a, b, c = new(conn, "a"), new(conn, "b"), new(conn, "c")
    ia, ib, ic = (plan.add(conn, t, TODAY, 30) for t in (a, b, c))
    assert plan.set_allotment(conn, ia, None).allotted_min is None
    plan.reorder(conn, TODAY, [ic, ia])
    assert [i.id for i in plan.items_on(conn, TODAY)] == [ic, ia, ib]
    removed = plan.remove_item(conn, ia)
    new_id = plan.reinsert(conn, removed)
    assert plan.get(conn, new_id).allotted_min is None
    with pytest.raises(ChangedSinceError):
        plan.reinsert(conn, removed)  # planned there again
    plan.add(conn, b, MON + timedelta(days=2), 10)
    plan.move_items(conn, [ib, ic], MON + timedelta(days=2))
    tomorrow = plan.items_on(conn, MON + timedelta(days=2))
    assert [(i.task_id, i.allotted_min) for i in tomorrow] == [(b, 10), (c, 30)]
    assert tasks.get(conn, b).moved_count == 1
    assert tasks.get(conn, c).moved_count == 1
    plan.move_items(conn, [tomorrow[1].id], MON + timedelta(days=2))  # same day: no-op
    assert tasks.get(conn, c).moved_count == 1
    with pytest.raises(NotFoundError):
        plan.get(conn, 999)


def test_reinsert_for_a_purged_task(conn):
    a = new(conn)
    item = plan.remove_item(conn, plan.add(conn, a, TODAY, 30))
    tasks.soft_delete(conn, a, NOW - timedelta(days=2))
    tasks.purge_deleted(conn, NOW)
    with pytest.raises(NotFoundError):
        plan.reinsert(conn, item)


# --- Repository: candidates ---------------------------------------------------------------


def test_candidates_with_reasons(conn):
    overdue = new(conn, "overdue", due_date=MON)
    due = new(conn, "due", due_date=TODAY)
    high = new(conn, "high", priority=Priority.P2)
    new(conn, "low", priority=Priority.P3)
    planned_high = new(conn, "planned high", priority=Priority.P1, plan=PlanRequest(TODAY, 30))
    later = new(
        conn, "later high", priority=Priority.P1, plan=PlanRequest(TODAY + timedelta(days=2), 30)
    )
    follow = new(conn, "follow")
    tasks.put_on_hold(conn, follow, Hold(HoldReason.SOMEONE, None, NOW, TODAY), TODAY, NOW)
    waiting = new(conn, "waiting")
    tasks.put_on_hold(
        conn, waiting, Hold(HoldReason.SOMEONE, None, NOW, TODAY + timedelta(days=3)), TODAY, NOW
    )
    found = tasks.list_tasks(conn, TaskQuery(ListKind.CANDIDATES, TODAY, plan_day=TODAY))
    ids = [s.task.id for s in found]
    assert set(ids) == {overdue, due, high, follow}
    assert planned_high not in ids
    assert later not in ids
    assert waiting not in ids
    reasons = {s.task.title: candidate_reason(s, TODAY) for s in found}
    assert reasons == {
        "overdue": CandidateReason.OVERDUE,
        "due": CandidateReason.DUE,
        "high": CandidateReason.PRIORITY,
        "follow": CandidateReason.FOLLOW_UP,
    }
    # Planning tomorrow: today's due task is overdue by then.
    tomorrow = tasks.list_tasks(
        conn, TaskQuery(ListKind.CANDIDATES, TODAY, plan_day=TODAY + timedelta(days=1))
    )
    # Already in today's plan: not an Inbox task, so it carries over instead.
    assert planned_high not in [s.task.id for s in tomorrow]
    assert (
        candidate_reason(next(s for s in tomorrow if s.task.id == due), TODAY + timedelta(days=1))
        == CandidateReason.OVERDUE
    )


def test_summaries_and_today_count(conn):
    a = new(conn, "a", plan=PlanRequest(TODAY, 30))
    b = new(conn, "b", plan=PlanRequest(TODAY, 30))
    tasks.complete(conn, b, NOW, TODAY)
    gone = new(conn, "gone")
    tasks.soft_delete(conn, gone, NOW)
    found = tasks.summaries(conn, [a, b, gone], TODAY)
    assert set(found) == {a, b}
    assert tasks.summaries(conn, [], TODAY) == {}
    assert tasks.counts(conn, TODAY, TODAY + timedelta(days=14)).today == 1


# --- Repository: recurring tasks ----------------------------------------------------------


def test_completing_a_repeating_task_creates_the_next_instance(conn):
    weekly = Recurrence(RecurrenceFreq.WEEKLY)
    task_id = new(conn, "Report", due_date=TODAY, tags=("ops",), recurrence=weekly, estimate_min=30)
    tasks.add_checklist_item(conn, task_id, "collect")
    tasks.set_checklist_done(conn, tasks.checklist(conn, task_id)[0].id, True)
    next_id = tasks.complete(conn, task_id, NOW, TODAY)
    nxt = tasks.get(conn, next_id)
    assert (nxt.due_date, nxt.status, nxt.recurs_from_id, nxt.recurrence) == (
        date(2026, 11, 10),
        TaskStatus.TO_DO,
        task_id,
        weekly,
    )
    assert (nxt.title, nxt.estimate_min) == ("Report", 30)
    assert tasks.tags_of(conn, next_id) == ("ops",)
    assert [(i.text, i.done) for i in tasks.checklist(conn, next_id)] == [("collect", False)]
    assert tasks.complete(conn, task_id, NOW, TODAY) is None  # already done


def test_a_repeating_task_without_due_date_and_an_ended_rule(conn):
    daily = new(conn, "x", recurrence=Recurrence(RecurrenceFreq.DAILY))
    assert tasks.get(conn, tasks.complete(conn, daily, NOW, TODAY)).due_date == TODAY + timedelta(
        days=1
    )
    ended = new(conn, "y", due_date=TODAY, recurrence=Recurrence(RecurrenceFreq.DAILY, until=TODAY))
    assert tasks.complete(conn, ended, NOW, TODAY) is None


def test_undo_of_a_repeating_completion_removes_the_next_instance(conn):
    task_id = new(conn, "x", due_date=TODAY, recurrence=Recurrence(RecurrenceFreq.DAILY))
    before = tasks.capture(conn, task_id, TODAY)
    next_id = tasks.complete(conn, task_id, NOW, TODAY)
    after = tasks.capture(conn, task_id, TODAY)
    created = tasks.capture(conn, next_id, TODAY)
    tasks.update(conn, replace(tasks.get(conn, next_id), notes="edited"), NOW)
    with pytest.raises(ChangedSinceError):
        tasks.restore(conn, before, after, remove=created)
    tasks.restore(conn, before, after, remove=tasks.capture(conn, next_id, TODAY))
    assert tasks.get(conn, task_id).status is TaskStatus.TO_DO
    with pytest.raises(NotFoundError):
        tasks.get(conn, next_id, include_deleted=True)


def test_damaged_recurrence_is_ignored(conn):
    task_id = new(conn, "x")
    conn.execute("UPDATE task SET recurrence = '{bad' WHERE id = ?", (task_id,))
    assert tasks.get(conn, task_id).recurrence is None


def test_update_changes_recurrence(conn):
    task = tasks.get(conn, new(conn, "x"))
    saved = tasks.update(conn, replace(task, recurrence=Recurrence(RecurrenceFreq.WEEKDAYS)), NOW)
    assert saved.recurrence == Recurrence(RecurrenceFreq.WEEKDAYS)


# --- Repository: meetings -----------------------------------------------------------------


def draft(**changes) -> MeetingDraft:
    base = {
        "title": "Standup",
        "start": datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        "duration_min": 15,
        "link": "https://meet/x",
        "recurrence": Recurrence(RecurrenceFreq.WEEKDAYS),
    }
    base.update(changes)
    return MeetingDraft(**base)


def test_meeting_create_update_and_exceptions(conn):
    meeting_id = meetings.create(conn, draft(), "Europe/Berlin", NOW)
    meeting = meetings.get(conn, meeting_id)
    assert (meeting.title, meeting.recurrence, meeting.reminder_offsets) == (
        "Standup",
        Recurrence(RecurrenceFreq.WEEKDAYS),
        (10, 1),
    )
    tuesday = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    meetings.set_occurrence(
        conn,
        meeting,
        tuesday,
        edit=OccurrenceEdit("Standup", tuesday + timedelta(hours=1), 15, "https://meet/x", (10, 1)),
    )
    change = meetings.exception_for(conn, meeting_id, tuesday)
    # Only what differs from the series is stored.
    assert (
        change.title,
        change.start,
        change.duration_min,
        change.link,
        change.reminder_offsets,
    ) == (
        None,
        tuesday + timedelta(hours=1),
        None,
        None,
        None,
    )
    meetings.set_occurrence(conn, meeting, tuesday, cancelled=True)
    assert meetings.exceptions(conn)[meeting_id][0].cancelled
    meetings.restore_exception(conn, meeting_id, tuesday, change)
    assert not meetings.exception_for(conn, meeting_id, tuesday).cancelled
    meetings.restore_exception(conn, meeting_id, tuesday, None)
    assert meetings.exception_for(conn, meeting_id, tuesday) is None
    with pytest.raises(ValidationError):
        meetings.set_occurrence(conn, meeting, tuesday + timedelta(minutes=5), cancelled=True)
    meetings.set_occurrence(conn, meeting, tuesday, cancelled=True)
    meetings.update_series(
        conn,
        meeting_id,
        draft(title="Daily"),
        NOW,
        discard=[meetings.exceptions(conn)[meeting_id][0].id],
    )
    assert meetings.get(conn, meeting_id).title == "Daily"
    assert meetings.exceptions(conn, meeting_id) == {}


def test_meeting_delete_purge_project_and_bad_rows(conn):
    with pytest.raises(ValidationError):
        meetings.create(conn, draft(project_id=99), "Europe/Berlin", NOW)
    meeting_id = meetings.create(conn, draft(project_id=1, recurrence=None), "Europe/Berlin", NOW)
    meetings.soft_delete(conn, meeting_id, NOW - timedelta(days=2))
    assert meetings.active(conn) == []
    with pytest.raises(NotFoundError):
        meetings.get(conn, meeting_id)
    meetings.undelete(conn, meeting_id)
    assert [m.id for m in meetings.active(conn)] == [meeting_id]
    meetings.soft_delete(conn, meeting_id, NOW - timedelta(days=2))
    assert meetings.purge_deleted(conn, NOW - timedelta(days=1)) == 1
    other = meetings.create(conn, draft(), "Europe/Berlin", NOW)
    conn.execute("UPDATE meeting SET recurrence = '{bad' WHERE id = ?", (other,))
    assert meetings.get(conn, other).recurrence is None


def test_adhoc_end_and_reopen(conn):
    meeting_id = meetings.create(
        conn,
        draft(recurrence=None, reminder_offsets=()),
        "UTC",
        NOW,
        is_adhoc=True,
        open_ended=True,
    )
    before = meetings.get(conn, meeting_id)
    meetings.end_adhoc(conn, meeting_id, 42, NOW)
    ended = meetings.get(conn, meeting_id)
    assert (ended.duration_min, ended.open_ended) == (42, False)
    meetings.reopen_adhoc(conn, before)
    assert meetings.get(conn, meeting_id).open_ended
    regular = meetings.create(conn, draft(), "UTC", NOW)
    with pytest.raises(ValueError, match="ad-hoc"):
        meetings.end_adhoc(conn, regular, 5, NOW)


# --- Repository: time off and leave -------------------------------------------------------


def test_time_off_rows(conn):
    time_off.put(conn, TimeOff(TODAY, TimeOffPart.AM, "  dentist "))
    assert time_off.get(conn, TODAY) == TimeOff(TODAY, TimeOffPart.AM, "dentist")
    time_off.put(conn, TimeOff(TODAY, TimeOffPart.FULL))
    assert time_off.all_days(conn) == {TODAY: TimeOffPart.FULL}
    assert [e.day for e in time_off.from_day(conn, TODAY + timedelta(days=1))] == []
    time_off.remove(conn, TODAY)
    assert time_off.get(conn, TODAY) is None


def test_marking_leave_moves_open_tasks_and_undo_puts_everything_back(conn):
    wed = TODAY + timedelta(days=1)
    thu = TODAY + timedelta(days=2)
    a, b, done = new(conn, "a"), new(conn, "b"), new(conn, "done")
    for task_id in (a, b, done):
        plan.add(conn, task_id, wed, 30)
    tasks.complete(conn, done, NOW, TODAY)
    before = leave.mark(conn, [TimeOff(wed, TimeOffPart.FULL)], lambda _d: thu, TODAY)
    assert sorted(s.task.id for s in before) == [a, b]
    assert [i.task_id for i in plan.items_on(conn, thu)] == [a, b]
    assert [i.task_id for i in plan.items_on(conn, wed)] == [done]
    assert tasks.get(conn, a).moved_count == 1
    after = [tasks.capture(conn, s.task.id, s.plan_from) for s in before]
    leave.undo(conn, {wed: None}, list(zip(before, after, strict=True)))
    assert time_off.get(conn, wed) is None
    assert sorted(i.task_id for i in plan.items_on(conn, wed)) == sorted([a, b, done])
    assert tasks.get(conn, a).moved_count == 0


def test_half_day_leave_moves_nothing(conn):
    a = new(conn, "a", plan=PlanRequest(TODAY, 30))
    assert leave.mark(conn, [TimeOff(TODAY, TimeOffPart.PM)], lambda d: d, TODAY) == []
    assert [i.task_id for i in plan.items_on(conn, TODAY)] == [a]
