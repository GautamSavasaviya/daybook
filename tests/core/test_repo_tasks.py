import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core.errors import ChangedSinceError, NotFoundError, ValidationError
from daybook.core.models import (
    Hold,
    HoldReason,
    PlanRequest,
    PlanSlot,
    Priority,
    ProjectDraft,
    TaskDraft,
    TaskStatus,
)
from daybook.core.repo import plan, projects, tasks, time_entries, time_off
from daybook.core.tasklists import ListKind, TaskFilter, TaskQuery

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)
TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


def new(conn, title="Task", **fields) -> int:
    return tasks.create(conn, TaskDraft(title, **fields), NOW)


def ids(conn, kind, **query) -> list[int]:
    return [s.task.id for s in tasks.list_tasks(conn, TaskQuery(kind, TODAY, **query))]


# --- Create, read, update -----------------------------------------------------------------


def test_create_with_only_a_title_uses_the_defaults(conn):
    task = tasks.get(conn, new(conn, "  Write   docs "))
    assert task.title == "Write docs"
    assert task.project_id == 1
    assert task.priority is Priority.P3
    assert task.status is TaskStatus.TO_DO
    assert task.hold is None
    assert task.created_at == task.updated_at == NOW
    assert tasks.tags_of(conn, task.id) == ()
    assert plan.items_for_task(conn, task.id, TODAY) == []


def test_create_with_every_field(conn):
    pid = projects.create(conn, ProjectDraft("PMS", key_prefix="PROJ"), NOW)
    task_id = new(
        conn,
        "Fix login",
        project_id=pid,
        priority=Priority.P1,
        ticket_key="proj-123",
        requested_by=" Rahul ",
        estimate_min=120,
        due_date=TOMORROW,
        notes="see logs\n",
        tags=("backend", "Backend", "api"),
        plan=PlanRequest(TODAY, None),
    )
    task = tasks.get(conn, task_id)
    assert (task.ticket_key, task.requested_by, task.estimate_min) == ("PROJ-123", "Rahul", 120)
    assert task.due_date == TOMORROW
    assert task.notes == "see logs"
    assert tasks.tags_of(conn, task_id) == ("api", "backend")
    [item] = plan.items_for_task(conn, task_id, TODAY)
    assert (item.plan_date, item.allotted_min, item.position) == (TODAY, None, 0)


def test_create_rejects_unknown_or_archived_projects_and_bad_allotments(conn):
    with pytest.raises(ValidationError) as info:
        new(conn, project_id=42)
    assert info.value.field == "project"
    pid = projects.create(conn, ProjectDraft("Old"), NOW)
    projects.set_archived(conn, pid, True)
    with pytest.raises(ValidationError):
        new(conn, project_id=pid)
    with pytest.raises(ValidationError) as info:
        new(conn, plan=PlanRequest(TODAY, 0))
    assert info.value.field == "allotment"
    assert conn.execute("SELECT count(*) FROM task").fetchone()[0] == 0  # nothing half-written


def test_update_saves_editable_fields_and_validates(conn):
    task = tasks.get(conn, new(conn))
    later = NOW + timedelta(minutes=5)
    saved = tasks.update(
        conn,
        replace(task, title=" New ", ticket_key="ab-1", priority=Priority.P2, due_date=TODAY),
        later,
    )
    assert (saved.title, saved.ticket_key, saved.priority, saved.due_date) == (
        "New",
        "AB-1",
        Priority.P2,
        TODAY,
    )
    assert saved.updated_at == later
    with pytest.raises(ValidationError):
        tasks.update(conn, replace(task, title=""), later)
    with pytest.raises(NotFoundError):
        tasks.update(conn, replace(task, id=99), later)


def test_task_may_stay_in_an_archived_project_but_not_move_into_one(conn):
    old = projects.create(conn, ProjectDraft("Old"), NOW)
    other = projects.create(conn, ProjectDraft("Other"), NOW)
    task = tasks.get(conn, new(conn, project_id=old))
    projects.set_archived(conn, old, True)
    tasks.update(conn, replace(task, title="Renamed"), NOW)
    projects.set_archived(conn, other, True)
    with pytest.raises(ValidationError):
        tasks.update(conn, replace(task, project_id=other), NOW)


# --- Status, hold, delete ---------------------------------------------------------------


def test_complete_and_reopen(conn):
    task_id = new(conn)
    tasks.complete(conn, task_id, NOW, TODAY)
    task = tasks.get(conn, task_id)
    assert task.status is TaskStatus.DONE
    assert task.completed_at == NOW
    tasks.complete(conn, task_id, NOW + timedelta(hours=1), TODAY)  # already done: unchanged
    assert tasks.get(conn, task_id).completed_at == NOW
    tasks.set_status(conn, task_id, TaskStatus.TO_DO, NOW)
    assert tasks.get(conn, task_id).completed_at is None
    with pytest.raises(ValueError, match="complete"):
        tasks.set_status(conn, task_id, TaskStatus.DONE, NOW)


def test_hold_removes_today_and_later_plans_but_keeps_history(conn):
    task_id = new(conn)
    plan.add(conn, task_id, YESTERDAY, 30)
    plan.add(conn, task_id, TODAY, 60)
    plan.add(conn, task_id, TOMORROW, None)
    hold = Hold(HoldReason.APPROVAL, "QA", NOW, TOMORROW)
    removed = tasks.put_on_hold(conn, task_id, hold, TODAY, NOW)
    assert [i.plan_date for i in removed] == [TODAY, TOMORROW]
    assert [i.plan_date for i in plan.items_for_task(conn, task_id, YESTERDAY)] == [YESTERDAY]
    task = tasks.get(conn, task_id)
    assert task.status is TaskStatus.ON_HOLD
    assert task.hold == hold
    tasks.set_status(conn, task_id, TaskStatus.IN_PROGRESS, NOW)
    task = tasks.get(conn, task_id)
    assert (task.status, task.hold) == (TaskStatus.IN_PROGRESS, None)


def test_completing_an_on_hold_task_clears_the_hold(conn):
    task_id = new(conn)
    tasks.put_on_hold(conn, task_id, Hold(HoldReason.OTHER, "x", NOW, TODAY), TODAY, NOW)
    tasks.complete(conn, task_id, NOW, TODAY)
    task = tasks.get(conn, task_id)
    assert (task.status, task.hold) == (TaskStatus.DONE, None)


def test_soft_delete_hides_and_undelete_restores(conn):
    task_id = new(conn)
    tasks.soft_delete(conn, task_id, NOW)
    with pytest.raises(NotFoundError):
        tasks.get(conn, task_id)
    assert tasks.get(conn, task_id, include_deleted=True).deleted_at == NOW
    assert ids(conn, ListKind.INBOX) == []
    assert ids(conn, ListKind.SEARCH, text="task") == []
    tasks.undelete(conn, task_id)
    assert tasks.get(conn, task_id).deleted_at is None
    with pytest.raises(NotFoundError):
        tasks.undelete(conn, 99)


def test_purge_removes_old_trash_with_everything_attached(conn):
    old, recent, kept = new(conn, "old"), new(conn, "recent"), new(conn, "kept")
    tasks.add_checklist_item(conn, old, "step")
    tasks.set_tags(conn, old, ["x"])
    plan.add(conn, old, TODAY, 30)
    conn.execute(
        "INSERT INTO time_entry (task_id, start_utc, end_utc, source) "
        "VALUES (?, '2026-11-03T08:00:00Z', '2026-11-03T08:30:00Z', 'manual')",
        (old,),
    )
    tasks.soft_delete(conn, old, NOW - timedelta(hours=25))
    tasks.soft_delete(conn, recent, NOW - timedelta(hours=1))
    assert tasks.purge_deleted(conn, NOW - timedelta(hours=24)) == 1
    with pytest.raises(NotFoundError):
        tasks.get(conn, old, include_deleted=True)
    tasks.get(conn, recent, include_deleted=True)
    tasks.get(conn, kept)
    for table in ("checklist_item", "task_tag", "day_plan_item", "time_entry", "tag"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


# --- Move -------------------------------------------------------------------------------


def test_moving_a_planned_task_keeps_its_allotment_and_counts(conn):
    task_id = new(conn, notes="first")
    plan.add(conn, task_id, TODAY, 45)
    later = date(2026, 11, 6)
    assert tasks.move(conn, task_id, TODAY, later, note="3 Nov: moved — API", now=NOW)
    [item] = plan.items_for_task(conn, task_id, TODAY)
    assert (item.plan_date, item.allotted_min) == (later, 45)
    task = tasks.get(conn, task_id)
    assert task.moved_count == 1
    assert task.notes == "first\n3 Nov: moved — API"


def test_planning_an_unplanned_task_is_not_a_move(conn):
    task_id = new(conn, estimate_min=90)
    assert not tasks.move(conn, task_id, TODAY, TOMORROW, note=None, now=NOW)
    [item] = plan.items_for_task(conn, task_id, TODAY)
    assert item.allotted_min == 90
    assert tasks.get(conn, task_id).moved_count == 0


def test_moving_to_unplanned_and_to_the_same_day(conn):
    task_id = new(conn)
    plan.add(conn, task_id, TODAY, None)
    assert not tasks.move(conn, task_id, TODAY, TODAY, note=None, now=NOW)  # nothing to do
    assert tasks.move(conn, task_id, TODAY, None, note=None, now=NOW)
    assert plan.items_for_task(conn, task_id, TODAY) == []
    assert tasks.get(conn, task_id).moved_count == 1


def test_moving_keeps_rest_of_day_and_appends_to_the_target_day(conn):
    other, task_id = new(conn, "other"), new(conn)
    plan.add(conn, other, TOMORROW, 30)
    plan.add(conn, task_id, TODAY, None)
    plan.add(conn, task_id, TOMORROW + timedelta(days=1), 20)
    tasks.move(conn, task_id, TODAY, TOMORROW, note=None, now=NOW)
    items = plan.items_on(conn, TOMORROW)
    assert [(i.task_id, i.allotted_min, i.position) for i in items] == [
        (other, 30, 0),
        (task_id, None, 1),
    ]
    assert len(plan.items_for_task(conn, task_id, TODAY)) == 1


# --- Undo snapshots ---------------------------------------------------------------------


def test_capture_and_restore_roll_back_status_hold_plan_and_deletion(conn):
    task_id = new(conn, notes="n")
    plan.add(conn, task_id, TODAY, 30)
    plan.add(conn, task_id, TOMORROW, None)
    state = tasks.capture(conn, task_id, TODAY)
    tasks.put_on_hold(conn, task_id, Hold(HoldReason.BLOCKED, None, NOW, None), TODAY, NOW)
    tasks.soft_delete(conn, task_id, NOW)
    tasks.restore(conn, state)
    assert tasks.get(conn, task_id) == state.task
    restored = plan.items_for_task(conn, task_id, TODAY)
    assert [(i.plan_date, i.allotted_min) for i in restored] == [(TODAY, 30), (TOMORROW, None)]


def test_restore_puts_a_hold_back(conn):
    task_id = new(conn)
    tasks.put_on_hold(conn, task_id, Hold(HoldReason.SOMEONE, "Anna", NOW, TOMORROW), TODAY, NOW)
    state = tasks.capture(conn, task_id, TODAY)
    tasks.complete(conn, task_id, NOW, TODAY)
    tasks.restore(conn, state)
    assert tasks.get(conn, task_id).hold == Hold(HoldReason.SOMEONE, "Anna", NOW, TOMORROW)


def test_restore_of_a_purged_task_raises(conn):
    task_id = new(conn)
    state = tasks.capture(conn, task_id, TODAY)
    tasks.soft_delete(conn, task_id, NOW - timedelta(days=2))
    tasks.purge_deleted(conn, NOW)
    with pytest.raises(NotFoundError):
        tasks.restore(conn, state)


# --- Tags and checklist -----------------------------------------------------------------


def test_tags_replace_and_unused_tags_disappear(conn):
    a, b = new(conn, "a"), new(conn, "b")
    tasks.set_tags(conn, a, ["ui", "api"])
    tasks.set_tags(conn, b, ["API"])
    assert tasks.tags_of(conn, b) == ("api",)  # the existing spelling wins
    assert tasks.all_tags(conn) == ["api", "ui"]
    tasks.set_tags(conn, a, [])
    assert tasks.all_tags(conn) == ["api"]
    tasks.soft_delete(conn, b, NOW)
    assert tasks.all_tags(conn) == []
    with pytest.raises(ValidationError):
        tasks.set_tags(conn, a, ["two words"])


def test_checklist_lifecycle(conn):
    task_id = new(conn)
    first = tasks.add_checklist_item(conn, task_id, " repro ")
    second = tasks.add_checklist_item(conn, task_id, "fix")
    assert [(i.text, i.position) for i in tasks.checklist(conn, task_id)] == [
        ("repro", 0),
        ("fix", 1),
    ]
    assert tasks.set_checklist_done(conn, first, True).done
    assert tasks.rename_checklist_item(conn, second, "fix it").text == "fix it"
    removed = tasks.delete_checklist_item(conn, first)
    assert [i.text for i in tasks.checklist(conn, task_id)] == ["fix it"]
    tasks.restore_checklist_item(conn, removed)
    assert [(i.text, i.done) for i in tasks.checklist(conn, task_id)] == [
        ("repro", True),
        ("fix it", False),
    ]
    with pytest.raises(ValidationError):
        tasks.add_checklist_item(conn, task_id, "  ")
    with pytest.raises(NotFoundError):
        tasks.set_checklist_done(conn, 999, True)
    with pytest.raises(NotFoundError):
        tasks.add_checklist_item(conn, 999, "x")


# --- Form lookups -----------------------------------------------------------------------


def test_requested_by_values_most_used_first(conn):
    for name in ("Anna", "Rahul", "rahul", "Priya", "Anna", "Rahul"):
        new(conn, requested_by=name)
    gone = new(conn, requested_by="Gone")
    tasks.soft_delete(conn, gone, NOW)
    values = tasks.requested_by_values(conn)
    assert [v.casefold() for v in values] == ["rahul", "anna", "priya"]


def test_ticket_key_in_use(conn):
    task_id = new(conn, ticket_key="PROJ-1")
    assert tasks.ticket_key_in_use(conn, "proj-1")
    assert not tasks.ticket_key_in_use(conn, "PROJ-1", exclude_id=task_id)
    assert not tasks.ticket_key_in_use(conn, "PROJ-2")


# --- Lists ------------------------------------------------------------------------------


def test_inbox_holds_active_unplanned_tasks_sorted_by_priority_then_due(conn):
    p3_late = new(conn, "p3 late", due_date=TODAY + timedelta(days=5))
    p3_none = new(conn, "p3 none")
    p3_soon = new(conn, "p3 soon", due_date=TOMORROW)
    p1 = new(conn, "p1", priority=Priority.P1)
    planned = new(conn, "planned", plan=PlanRequest(TOMORROW, 30))
    past_plan = new(conn, "planned yesterday")
    plan.add(conn, past_plan, YESTERDAY, 30)
    held = new(conn, "held")
    tasks.put_on_hold(conn, held, Hold(HoldReason.OTHER, None, NOW, None), TODAY, NOW)
    done = new(conn, "done")
    tasks.complete(conn, done, NOW, TODAY)
    assert ids(conn, ListKind.INBOX) == [p1, p3_soon, p3_late, p3_none, past_plan]
    assert planned not in ids(conn, ListKind.INBOX)


def test_archived_projects_drop_out_of_active_lists_but_stay_searchable(conn):
    pid = projects.create(conn, ProjectDraft("Old"), NOW)
    task_id = new(conn, "legacy thing", project_id=pid, due_date=TOMORROW)
    done = new(conn, "legacy done", project_id=pid)
    tasks.complete(conn, done, NOW, TODAY)
    projects.set_archived(conn, pid, True)
    assert ids(conn, ListKind.INBOX) == []
    assert ids(conn, ListKind.UPCOMING) == []
    assert ids(conn, ListKind.SEARCH, text="legacy") == [task_id, done]
    assert ids(conn, ListKind.DONE) == [done]
    [summary] = tasks.list_tasks(conn, TaskQuery(ListKind.DONE, TODAY))
    assert summary.project_archived


def test_upcoming_covers_due_planned_overdue_and_on_hold(conn):
    due_soon = new(conn, "due soon", due_date=TOMORROW)
    due_far = new(conn, "due far", due_date=TODAY + timedelta(days=14))
    planned = new(conn, "planned", plan=PlanRequest(TODAY + timedelta(days=3), 30))
    planned_far = new(conn, "planned far", plan=PlanRequest(TODAY + timedelta(days=20), 30))
    overdue = new(conn, "overdue", due_date=YESTERDAY)
    held = new(conn, "held")
    tasks.put_on_hold(conn, held, Hold(HoldReason.OTHER, None, NOW, None), TODAY, NOW)
    plain = new(conn, "plain")
    result = ids(conn, ListKind.UPCOMING)
    assert set(result) == {due_soon, planned, overdue, held}
    assert not {due_far, planned_far, plain} & set(result)


def test_summary_carries_project_tags_next_plan_and_checklist(conn):
    pid = projects.create(conn, ProjectDraft("PMS", key_prefix="PROJ"), NOW)
    task_id = new(conn, project_id=pid, tags=("b", "a"))
    plan.add(conn, task_id, YESTERDAY, 15)
    plan.add(conn, task_id, TODAY + timedelta(days=2), None)
    plan.add(conn, task_id, TODAY + timedelta(days=5), 60)
    conn.execute("UPDATE day_plan_item SET carry_count = 2 WHERE allotted_min IS NULL")
    first = tasks.add_checklist_item(conn, task_id, "x")
    tasks.add_checklist_item(conn, task_id, "y")
    tasks.set_checklist_done(conn, first, True)
    [summary] = tasks.list_tasks(conn, TaskQuery(ListKind.PROJECT, TODAY, project_id=pid))
    assert summary.project_name == "PMS"
    assert summary.tags == ("a", "b")
    assert summary.next_plan == PlanSlot(TODAY + timedelta(days=2), None, 2)
    assert (summary.checklist_done, summary.checklist_total) == (1, 2)


def test_project_list_shows_every_open_status(conn):
    pid = projects.create(conn, ProjectDraft("P"), NOW)
    todo = new(conn, project_id=pid)
    held = new(conn, project_id=pid)
    tasks.put_on_hold(conn, held, Hold(HoldReason.OTHER, None, NOW, None), TODAY, NOW)
    done = new(conn, project_id=pid)
    tasks.complete(conn, done, NOW, TODAY)
    new(conn, "elsewhere")
    assert ids(conn, ListKind.PROJECT, project_id=pid) == [todo, held]
    with pytest.raises(ValueError, match="project_id"):
        ids(conn, ListKind.PROJECT)


def test_done_list_is_newest_first(conn):
    first, second = new(conn, "first"), new(conn, "second")
    tasks.complete(conn, first, NOW + timedelta(hours=2), TODAY)
    tasks.complete(conn, second, NOW + timedelta(hours=1), TODAY)
    assert ids(conn, ListKind.DONE) == [first, second]


def test_search_matches_all_words_in_title_ticket_or_notes_unicode_aware(conn):
    title = new(conn, "Größe der Datei prüfen")
    ticket = new(conn, "Something", ticket_key="WEB-77")
    notes = new(conn, "Other", notes="check the Login flow")
    done = new(conn, "Login page")
    tasks.complete(conn, done, NOW, TODAY)
    assert ids(conn, ListKind.SEARCH, text="GRÖSSE") == [title]  # casefold: ß matches SS
    assert ids(conn, ListKind.SEARCH, text="größe") == [title]
    assert ids(conn, ListKind.SEARCH, text="PRÜFEN datei") == [title]
    assert ids(conn, ListKind.SEARCH, text="web-7") == [ticket]
    assert ids(conn, ListKind.SEARCH, text="login") == [notes, done]  # open before done
    assert ids(conn, ListKind.SEARCH, text="   ") == []
    assert ids(conn, ListKind.SEARCH, text="100%") == []


@pytest.mark.parametrize(
    ("task_filter", "expected"),
    [
        (TaskFilter(), {"a", "b", "c"}),
        (TaskFilter(priority=Priority.P1), {"a"}),
        (TaskFilter(status=TaskStatus.IN_PROGRESS), {"b"}),
        (TaskFilter(tag="UI"), {"a", "c"}),
        (TaskFilter(requested_by="rahul"), {"c"}),
        (TaskFilter(tag="ui", priority=Priority.P3), {"c"}),
    ],
)
def test_filters_narrow_any_list(conn, task_filter, expected):
    new(conn, "a", priority=Priority.P1, tags=("ui",))
    b = new(conn, "b")
    tasks.set_status(conn, b, TaskStatus.IN_PROGRESS, NOW)
    new(conn, "c", tags=("ui",), requested_by="Rahul")
    query = TaskQuery(ListKind.INBOX, TODAY, task_filter)
    found = {s.task.title for s in tasks.list_tasks(conn, query)}
    assert found == expected


def test_project_filter(conn):
    pid = projects.create(conn, ProjectDraft("P"), NOW)
    in_project = new(conn, project_id=pid)
    new(conn)
    assert ids(conn, ListKind.INBOX, filter=TaskFilter(project_id=pid)) == [in_project]


def test_counts_match_the_lists(conn):
    pid = projects.create(conn, ProjectDraft("P"), NOW)
    new(conn, project_id=pid)
    new(conn, due_date=TOMORROW)
    new(conn, plan=PlanRequest(TODAY, 30))
    held = new(conn, project_id=pid)
    tasks.put_on_hold(conn, held, Hold(HoldReason.OTHER, None, NOW, None), TODAY, NOW)
    done = new(conn)
    tasks.complete(conn, done, NOW, TODAY)
    horizon = TODAY + timedelta(days=14)
    counts = tasks.counts(conn, TODAY, horizon)
    assert counts.inbox == len(ids(conn, ListKind.INBOX)) == 2
    assert counts.upcoming == 2  # on hold is listed in Upcoming but not counted
    assert counts.per_project == {pid: 2, 1: 2}


# --- Plan items and tracked time --------------------------------------------------------


def test_plan_add_twice_replaces_the_allotment_in_place(conn):
    a, b = new(conn, "a"), new(conn, "b")
    first = plan.add(conn, a, TODAY, 30)
    plan.add(conn, b, TODAY, 15)
    assert plan.add(conn, a, TODAY, 60) == first
    assert [(i.task_id, i.allotted_min, i.position) for i in plan.items_on(conn, TODAY)] == [
        (a, 60, 0),
        (b, 15, 1),
    ]


def test_plan_items_read_carry_fields(conn):
    task_id = new(conn)
    plan.add(conn, task_id, TODAY, 30)
    conn.execute("UPDATE day_plan_item SET carry_count = 1, carried_from = '2026-11-02'")
    [item] = plan.items_on(conn, TODAY)
    assert (item.carry_count, item.carried_from) == (1, YESTERDAY)
    plan.restore(conn, task_id, TODAY, [item])
    [again] = plan.items_on(conn, TODAY)
    assert (again.carry_count, again.carried_from) == (1, YESTERDAY)


def add_entry(conn, task_id, start, end, source="manual"):
    conn.execute(
        "INSERT INTO time_entry (task_id, start_utc, end_utc, source) VALUES (?, ?, ?, ?)",
        (task_id, start, end, source),
    )


def test_tracked_time_clips_today_to_the_local_day_and_counts_the_live_timer(conn):
    task_id = new(conn)
    # 22:30 to 23:30 UTC on 2 Nov is 23:30 to 00:30 in Berlin: half of it is "today".
    add_entry(conn, task_id, "2026-11-02T22:30:00Z", "2026-11-02T23:30:00Z")
    add_entry(conn, task_id, "2026-11-03T07:00:00Z", "2026-11-03T07:15:00Z")
    now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO timer_state (id, task_id, state, segment_start_utc, last_seen_utc, "
        "updated_at) VALUES (1, ?, 'running', '2026-11-03T08:50:00Z', "
        "'2026-11-03T08:59:00Z', '2026-11-03T08:59:00Z')",
        (task_id,),
    )
    result = time_entries.tracked(conn, task_id, TODAY, BERLIN, now)
    assert result.today_s == 30 * 60 + 15 * 60 + 10 * 60
    assert result.total_s == 60 * 60 + 15 * 60 + 10 * 60
    assert time_entries.total_seconds(conn, task_id) == 75 * 60
    other = new(conn, "other")
    assert time_entries.tracked(conn, other, TODAY, BERLIN, now).total_s == 0


def test_time_off_days_are_read(conn):
    conn.execute("INSERT INTO time_off (day, part) VALUES ('2026-11-09', 'full')")
    assert time_off.all_days(conn) == {date(2026, 11, 9): "full"}


def test_fold_function_is_registered(conn):
    assert conn.execute("SELECT fold('ÄBC'), fold(NULL)").fetchone()[:] == ("äbc", None)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task (title, created_at, updated_at) VALUES (' ', 'x', 'x')")


def test_restore_with_expected_state_refuses_when_the_task_changed_since(conn):
    task_id = new(conn, notes="before")
    plan.add(conn, task_id, TODAY, 30)
    before = tasks.capture(conn, task_id, TODAY)
    tasks.complete(conn, task_id, NOW, TODAY)
    after = tasks.capture(conn, task_id, TODAY)
    tasks.update(conn, replace(tasks.get(conn, task_id), notes="edited later"), NOW)
    with pytest.raises(ChangedSinceError):
        tasks.restore(conn, before, expected=after)
    assert tasks.get(conn, task_id).notes == "edited later"  # nothing was overwritten
    assert tasks.get(conn, task_id).status is TaskStatus.DONE


def test_restore_with_expected_state_refuses_when_the_plan_changed_since(conn):
    task_id = new(conn)
    plan.add(conn, task_id, TODAY, 30)
    before = tasks.capture(conn, task_id, TODAY)
    tasks.complete(conn, task_id, NOW, TODAY)
    after = tasks.capture(conn, task_id, TODAY)
    plan.add(conn, task_id, TOMORROW, 15)
    with pytest.raises(ChangedSinceError):
        tasks.restore(conn, before, expected=after)
    tasks.restore(conn, before, expected=tasks.capture(conn, task_id, TODAY))
    assert tasks.get(conn, task_id).status is TaskStatus.TO_DO
