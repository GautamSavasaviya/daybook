"""WorkCalendar, hold rules, Move to…, tickets and autocomplete matching."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import hold, reschedule, suggest, tickets
from daybook.core.calendar import WorkCalendar
from daybook.core.errors import ValidationError
from daybook.core.models import (
    Hold,
    HoldReason,
    Priority,
    Project,
    ProjectColor,
    Task,
    TaskStatus,
    TimeOffPart,
)
from daybook.core.reschedule import MoveChoice, MoveKind
from daybook.core.settings import Settings

BERLIN = ZoneInfo("Europe/Berlin")
TUE = date(2026, 11, 3)
FRI = date(2026, 11, 6)
SAT = date(2026, 11, 7)
MON = date(2026, 11, 9)
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

WEEKDAYS = frozenset(range(5))


# --- WorkCalendar -----------------------------------------------------------------------


def test_weekends_are_not_working_days():
    cal = WorkCalendar(WEEKDAYS)
    assert cal.is_working(TUE)
    assert not cal.is_working(SAT)
    assert cal.next_working_day(FRI) == MON
    assert cal.prev_working_day(MON) == FRI
    assert cal.next_working_day(TUE) == TUE + timedelta(days=1)
    assert cal.working_on_or_after(SAT) == MON
    assert cal.working_on_or_after(TUE) == TUE


def test_full_day_off_is_skipped_but_half_day_is_still_working():
    cal = WorkCalendar(WEEKDAYS, {MON: TimeOffPart.FULL, FRI: TimeOffPart.PM})
    assert cal.is_working(FRI)
    assert cal.time_off(FRI) is TimeOffPart.PM
    assert not cal.is_working(MON)
    assert cal.next_working_day(FRI) == MON + timedelta(days=1)
    assert cal.prev_working_day(MON + timedelta(days=1)) == FRI
    assert cal.time_off(TUE) is None


def test_long_leave_on_a_one_day_week():
    mondays = [MON + timedelta(weeks=n) for n in range(5)]
    cal = WorkCalendar(frozenset({0}), dict.fromkeys(mondays, TimeOffPart.FULL))
    assert cal.next_working_day(TUE) == MON + timedelta(weeks=5)
    assert cal.prev_working_day(MON + timedelta(weeks=5)) == MON - timedelta(weeks=1)


def test_calendar_from_settings_and_empty_workdays():
    cal = WorkCalendar.from_settings(Settings(workdays=frozenset({5})))
    assert cal.is_working(SAT)
    assert not cal.is_working(FRI)
    with pytest.raises(ValueError, match="workday"):
        WorkCalendar(frozenset())


# --- Hold -------------------------------------------------------------------------------


def make_task(**changes) -> Task:
    base = {
        "id": 1,
        "project_id": 1,
        "title": "Task",
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
    base.update(changes)
    return Task(**base)


@pytest.mark.parametrize("reason", list(HoldReason))
def test_every_reason_has_labels(reason):
    assert hold.reason_label(reason)
    assert hold.reason_short(reason)
    assert " " not in hold.reason_short(reason)


def test_validate_hold_cleans_note_and_checks_follow_up():
    request = hold.HoldRequest(HoldReason.APPROVAL, "  waiting  for QA ", TUE)
    assert hold.validate(request, TUE) == hold.HoldRequest(
        HoldReason.APPROVAL, "waiting for QA", TUE
    )
    with pytest.raises(ValidationError) as info:
        hold.validate(hold.HoldRequest(HoldReason.OTHER, None, TUE - timedelta(days=1)), TUE)
    assert info.value.field == "follow_up"


def test_days_waiting_counts_local_days():
    # 23:30 Berlin on Monday is 22:30 UTC: one local day before Tuesday.
    since = datetime(2026, 11, 2, 22, 30, tzinfo=UTC)
    h = Hold(HoldReason.BLOCKED, None, since, None)
    assert hold.days_waiting(h, TUE, BERLIN) == 1
    assert hold.days_waiting(h, TUE, UTC) == 1
    assert hold.days_waiting(h, date(2026, 11, 2), BERLIN) == 0
    assert hold.days_waiting(h, date(2026, 11, 1), BERLIN) == 0  # clock went backwards


def test_follow_up_due_only_for_on_hold_tasks_on_or_after_the_date():
    held = make_task(status=TaskStatus.ON_HOLD, hold=Hold(HoldReason.SOMEONE, None, NOW, FRI))
    assert not hold.follow_up_due(held, TUE)
    assert hold.follow_up_due(held, FRI)
    assert hold.follow_up_due(held, MON)
    no_date = make_task(status=TaskStatus.ON_HOLD, hold=Hold(HoldReason.SOMEONE, None, NOW, None))
    assert not hold.follow_up_due(no_date, MON)
    assert not hold.follow_up_due(make_task(), MON)


def test_started_hold_records_now():
    request = hold.HoldRequest(HoldReason.POSTPONED, "later", FRI)
    assert hold.started(request, NOW) == Hold(HoldReason.POSTPONED, "later", NOW, FRI)


# --- Move to… ---------------------------------------------------------------------------


def test_quick_choice_is_tomorrow_on_weekdays_and_next_working_day_before_a_weekend():
    cal = WorkCalendar(WEEKDAYS)
    assert reschedule.quick_choice(TUE, cal) == MoveChoice(MoveKind.TOMORROW)
    assert reschedule.quick_choice(FRI, cal) == MoveChoice(MoveKind.NEXT_WORKING_DAY)
    assert reschedule.resolve(MoveChoice(MoveKind.TOMORROW), TUE, cal) == TUE + timedelta(days=1)
    assert reschedule.resolve(MoveChoice(MoveKind.NEXT_WORKING_DAY), FRI, cal) == MON


def test_tomorrow_skips_leave():
    cal = WorkCalendar(WEEKDAYS, {TUE + timedelta(days=1): TimeOffPart.FULL})
    assert reschedule.quick_choice(TUE, cal).kind is MoveKind.NEXT_WORKING_DAY
    assert reschedule.resolve(MoveChoice(MoveKind.TOMORROW), TUE, cal) == TUE + timedelta(days=2)


def test_resolve_a_picked_date():
    cal = WorkCalendar(WEEKDAYS)
    assert reschedule.resolve(MoveChoice(MoveKind.DATE, FRI), TUE, cal) == FRI
    assert reschedule.resolve(MoveChoice(MoveKind.DATE, TUE), TUE, cal) == TUE
    assert reschedule.resolve(MoveChoice(MoveKind.UNPLANNED), TUE, cal) is None


@pytest.mark.parametrize(
    ("day", "fragment"),
    [(None, "Pick a date"), (date(2026, 11, 2), "later"), (SAT, "Mon 9 Nov 2026")],
)
def test_resolve_refuses_bad_dates_with_a_suggestion(day, fragment):
    with pytest.raises(ValidationError) as info:
        reschedule.resolve(MoveChoice(MoveKind.DATE, day), TUE, WorkCalendar(WEEKDAYS))
    assert info.value.field == "move_date"
    assert fragment in info.value.message


def test_move_note_line_and_append():
    line = reschedule.note_line(TUE, TUE + timedelta(days=1), " waiting for API ")
    assert line == "3 Nov: moved to tomorrow — waiting for API"
    assert reschedule.note_line(TUE, None, "no time") == "3 Nov: moved to unplanned — no time"
    assert reschedule.note_line(TUE, FRI, "  ") is None
    assert reschedule.append_note("", line) == line
    assert reschedule.append_note("old\n\n", line) == f"old\n{line}"
    assert reschedule.append_note("old", None) == "old"


def test_default_allotment():
    assert reschedule.default_allotment(None) == 30
    assert reschedule.default_allotment(0) == 30
    assert reschedule.default_allotment(90) == 90
    assert reschedule.default_allotment(40 * 60) == 24 * 60


# --- Tickets ----------------------------------------------------------------------------


def project(pid, prefix=None, url=None, archived=False) -> Project:
    return Project(pid, f"P{pid}", ProjectColor.BLUE, prefix, url, archived, 0, NOW)


def test_project_for_key_matches_active_prefix():
    projects = [project(1), project(2, "PROJ"), project(3, "OLD", archived=True)]
    assert tickets.project_for_key("proj-12", projects) == projects[1]
    assert tickets.project_for_key("OLD-1", projects) is None
    assert tickets.project_for_key("#12", projects) is None
    assert tickets.project_for_key("NOPE-1", projects) is None


def test_ticket_url_prefers_own_project_then_prefix_and_escapes_the_key():
    pms = project(2, "PROJ", "https://pms/browse/{key}")
    other = project(3, None, "https://tracker/{key}/view")
    area = project(4)
    assert tickets.ticket_url("PROJ-1", pms, [pms]) == "https://pms/browse/PROJ-1"
    assert tickets.ticket_url("PROJ-1", other, [pms, other]) == "https://tracker/PROJ-1/view"
    assert tickets.ticket_url("PROJ-1", area, [area, pms]) == "https://pms/browse/PROJ-1"
    assert tickets.ticket_url("#7 x", other, [other]) == "https://tracker/%237%20x/view"
    assert tickets.ticket_url("ZZ-1", area, [area, pms]) == ""
    assert tickets.ticket_url(None, pms, [pms]) == ""
    assert tickets.ticket_url("PROJ-1", None, []) == ""


# --- Autocomplete -----------------------------------------------------------------------


def test_suggestions_match_word_starts_in_given_order():
    names = ["Rahul Shah", "Priya", "Raj", "Anna Rao"]
    assert suggest.matching(names, "ra") == ["Rahul Shah", "Raj", "Anna Rao"]
    assert suggest.matching(names, "SHA") == ["Rahul Shah"]
    assert suggest.matching(names, "raj") == []  # exact match: nothing to complete
    assert suggest.matching(names, " ") == []
    assert suggest.matching(names, "r", limit=1) == ["Rahul Shah"]
    assert suggest.matching(names, "hul") == []
