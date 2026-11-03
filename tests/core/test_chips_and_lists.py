from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import timeutil
from daybook.core.chips import ChipKind, ChipStyle, task_chips
from daybook.core.models import (
    Hold,
    HoldReason,
    PlanSlot,
    Priority,
    ProjectColor,
    Task,
    TaskStatus,
    TaskSummary,
)
from daybook.core.sources import ExternalTask, TaskSource
from daybook.core.tasklists import ListKind, SectionKind, TaskFilter, TaskQuery, sections

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 3)


def summary(task_id=1, **task_fields) -> TaskSummary:
    extra = {
        key: task_fields.pop(key)
        for key in ("tags", "next_plan", "checklist_done", "checklist_total")
        if key in task_fields
    }
    base = {
        "id": task_id,
        "project_id": 1,
        "title": f"Task {task_id}",
        "ticket_key": None,
        "requested_by": None,
        "priority": Priority.P3,
        "status": TaskStatus.TO_DO,
        "hold": None,
        "estimate_min": None,
        "due_date": None,
        "notes": "",
        "moved_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
        "deleted_at": None,
    }
    base.update(task_fields)
    return TaskSummary(Task(**base), "General", ProjectColor.SLATE, False, **extra)


def chips(s: TaskSummary):
    return {chip.kind: chip for chip in task_chips(s, TODAY, BERLIN)}


# --- Chips ------------------------------------------------------------------------------


def test_plain_p3_task_has_no_chips():
    assert task_chips(summary(), TODAY, BERLIN) == ()


@pytest.mark.parametrize(
    ("priority", "label", "style"),
    [
        (Priority.P1, "P1", ChipStyle.ERROR),
        (Priority.P2, "P2", ChipStyle.WARNING),
        (Priority.P4, "P4", ChipStyle.DIM),
    ],
)
def test_priority_chip_except_for_p3(priority, label, style):
    chip = chips(summary(priority=priority))[ChipKind.PRIORITY]
    assert (chip.label, chip.style) == (label, style)
    assert chip.description == f"Priority {priority.value}"


@pytest.mark.parametrize(
    ("due", "kind", "label"),
    [
        (TODAY - timedelta(days=2), ChipKind.OVERDUE, "overdue 2d"),
        (TODAY, ChipKind.DUE_TODAY, "due today"),
        (TODAY + timedelta(days=1), ChipKind.DUE, "due tomorrow"),
        (TODAY + timedelta(days=3), ChipKind.DUE, "due Fri"),
        (date(2026, 12, 24), ChipKind.DUE, "due Thu 24 Dec"),
    ],
)
def test_due_chips(due, kind, label):
    assert chips(summary(due_date=due))[kind].label == label


def test_overdue_description_is_plural_aware():
    assert chips(summary(due_date=TODAY - timedelta(days=1)))[ChipKind.OVERDUE].description == (
        "Overdue by 1 day"
    )


def test_on_hold_chip_shows_reason_and_days_and_follow_up():
    held = summary(
        status=TaskStatus.ON_HOLD,
        hold=Hold(HoldReason.APPROVAL, None, NOW - timedelta(days=3), TODAY),
    )
    result = chips(held)
    assert result[ChipKind.ON_HOLD].label == "on hold · approval · 3d"
    assert result[ChipKind.ON_HOLD].description == (
        "On hold: Waiting for approval or review, waiting 3 days"
    )
    assert ChipKind.FOLLOW_UP in result


def test_planned_and_carried_chips():
    today_plan = chips(summary(next_plan=PlanSlot(TODAY, 90, 2)))
    assert today_plan[ChipKind.PLANNED].label == "today · 1h 30m"
    assert today_plan[ChipKind.PLANNED].style is ChipStyle.ACCENT
    assert today_plan[ChipKind.CARRIED].label == "carried ×2"
    later = chips(summary(next_plan=PlanSlot(TODAY + timedelta(days=2), None, 1)))
    assert later[ChipKind.PLANNED].label == "Thu · rest of day"
    assert ChipKind.CARRIED not in later  # carried only matters for today's plan


def test_moved_chip_from_the_second_move():
    assert ChipKind.MOVED not in chips(summary(moved_count=1))
    assert chips(summary(moved_count=3))[ChipKind.MOVED].label == "moved ×3"


def test_requested_by_checklist_and_tags():
    result = task_chips(
        summary(requested_by="Rahul", checklist_done=1, checklist_total=3, tags=("api", "ui")),
        TODAY,
        BERLIN,
    )
    assert [c.label for c in result] == ["from Rahul", "1/3", "#api", "#ui"]
    assert result[0].description == "Requested by Rahul"
    assert result[0].person == "Rahul"


def test_done_task_shows_when_and_hides_due_and_plan():
    done = summary(
        status=TaskStatus.DONE,
        completed_at=NOW - timedelta(days=1),
        due_date=TODAY - timedelta(days=5),
        next_plan=PlanSlot(TODAY, 30, 0),
    )
    result = chips(done)
    assert result[ChipKind.DONE].label == "done yesterday"
    assert ChipKind.OVERDUE not in result
    assert ChipKind.PLANNED not in result


# --- Sections ---------------------------------------------------------------------------


def kinds(result):
    return [(s.kind, s.day, [i.task.id for i in s.items]) for s in result]


def test_inbox_is_one_section_or_none():
    assert kinds(sections(ListKind.INBOX, [summary(1), summary(2)], TODAY, BERLIN)) == [
        (SectionKind.ALL, None, [1, 2])
    ]
    assert sections(ListKind.INBOX, [], TODAY, BERLIN) == ()


def test_upcoming_groups_overdue_days_and_on_hold():
    tomorrow = TODAY + timedelta(days=1)
    items = [
        summary(1, due_date=tomorrow),
        summary(2, next_plan=PlanSlot(TODAY, 30, 0), due_date=tomorrow),
        summary(3, due_date=TODAY - timedelta(days=1)),
        summary(4, status=TaskStatus.ON_HOLD, hold=Hold(HoldReason.OTHER, None, NOW, None)),
        summary(5, status=TaskStatus.ON_HOLD, hold=Hold(HoldReason.OTHER, None, NOW, tomorrow)),
        summary(6, due_date=TODAY + timedelta(days=30), next_plan=PlanSlot(tomorrow, 30, 0)),
        summary(7, due_date=TODAY + timedelta(days=30)),  # outside the window: not listed
    ]
    assert kinds(sections(ListKind.UPCOMING, items, TODAY, BERLIN)) == [
        (SectionKind.OVERDUE, None, [3]),
        (SectionKind.DAY, TODAY, [2]),
        (SectionKind.DAY, tomorrow, [1, 6]),
        (SectionKind.ON_HOLD, None, [5, 4]),
    ]


def test_project_sections_by_status():
    items = [
        summary(1),
        summary(2, status=TaskStatus.IN_PROGRESS),
        summary(3, status=TaskStatus.ON_HOLD, hold=Hold(HoldReason.OTHER, None, NOW, None)),
        summary(4, status=TaskStatus.DONE, completed_at=NOW),
    ]
    assert [s.kind for s in sections(ListKind.PROJECT, items, TODAY, BERLIN)] == [
        SectionKind.IN_PROGRESS,
        SectionKind.TO_DO,
        SectionKind.ON_HOLD,
        SectionKind.DONE,
    ]


def test_done_groups_by_local_completion_day_newest_first():
    late_evening = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)  # 00:30 on 3 Nov in Berlin
    items = [
        summary(1, status=TaskStatus.DONE, completed_at=NOW),
        summary(2, status=TaskStatus.DONE, completed_at=late_evening),
        summary(3, status=TaskStatus.DONE, completed_at=NOW - timedelta(days=2)),
    ]
    assert kinds(sections(ListKind.DONE, items, TODAY, BERLIN)) == [
        (SectionKind.DONE, TODAY, [1, 2]),
        (SectionKind.DONE, date(2026, 11, 1), [3]),
    ]


def test_search_splits_open_and_done():
    items = [summary(1), summary(2, status=TaskStatus.DONE, completed_at=NOW)]
    assert kinds(sections(ListKind.SEARCH, items, TODAY, BERLIN)) == [
        (SectionKind.OPEN, None, [1]),
        (SectionKind.DONE, None, [2]),
    ]


def test_filter_activity_and_query_horizon():
    assert not TaskFilter().active
    assert TaskFilter(tag="x").active
    assert replace(TaskFilter(), priority=Priority.P1).active
    assert TaskQuery(ListKind.UPCOMING, TODAY).horizon == date(2026, 11, 17)


# --- Day formatting ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "text"),
    [
        (TODAY, "today"),
        (TODAY + timedelta(days=1), "tomorrow"),
        (TODAY - timedelta(days=1), "yesterday"),
        (TODAY + timedelta(days=2), "Thu"),
        (TODAY + timedelta(days=6), "Mon"),
        (TODAY + timedelta(days=7), "Tue 10 Nov"),
        (TODAY - timedelta(days=3), "Sat 31 Oct"),
        (date(2027, 1, 5), "Tue 5 Jan 2027"),
    ],
)
def test_fmt_day(day, text):
    assert timeutil.fmt_day(day, TODAY) == text


def test_fmt_date_and_day_month():
    assert timeutil.fmt_date(TODAY) == "Tue 3 Nov 2026"
    assert timeutil.fmt_day_month(TODAY) == "3 Nov"


# --- TaskSource -------------------------------------------------------------------------


def test_a_plugin_class_satisfies_the_task_source_protocol():
    class FakeJira:
        source_id = "jira:test"
        display_name = "Jira"

        def key_prefixes(self):
            return ["PROJ"]

        def fetch_assigned(self):
            return [ExternalTask("PROJ-1", "Fix")]

        def fetch(self, key):
            return None

    source = FakeJira()
    assert isinstance(source, TaskSource)
    assert source.fetch_assigned()[0].key == "PROJ-1"
    assert not isinstance(object(), TaskSource)
