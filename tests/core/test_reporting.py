"""Timesheet rounding and totals (mandatory, SPEC), the standup and the weekly report."""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import reports, standup, timesheet
from daybook.core.models import Occurrence, TimeSlice
from daybook.core.reports import PlannedItem

BERLIN = ZoneInfo("Europe/Berlin")
MON, TUE = date(2026, 11, 2), date(2026, 11, 3)
WEEK = timesheet.week_days(TUE)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def piece(task_id, day, start_h, minutes, *, ticket="PROJ-1", title="Fix it", project="PMS", pid=2):
    start = at(day, start_h)
    return TimeSlice(
        task_id, ticket, title, pid, project, start, start + timedelta(minutes=minutes)
    )


# --- Rounding -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "step", "expected"),
    [
        (0, 15, 0),
        (1, 15, 15),  # any time at all is at least one increment (D-028)
        (7 * 60, 15, 15),
        (7 * 60 + 29, 15, 15),
        (22 * 60 + 30, 15, 30),  # halves round up
        (37 * 60, 15, 30),
        (38 * 60, 15, 45),
        (53 * 60, 15, 60),
        (90 * 60, 15, 90),
        (20 * 60, 30, 30),
        (10 * 60, 0, 10),  # off: whole minutes
        (20, 0, 1),
        (89 * 60 + 40, 0, 90),
    ],
)
def test_round_minutes(seconds, step, expected):
    assert timesheet.round_minutes(seconds, step) == expected


# --- Timesheet ------------------------------------------------------------------------------


def test_rows_per_ticket_merge_tasks_and_split_by_day():
    slices = [
        piece(1, MON, 9, 50),
        piece(1, MON, 11, 20),  # same cell: 70 min
        piece(2, TUE, 9, 30, title="Second task, same ticket"),  # same ticket key
        piece(3, TUE, 10, 40, ticket=None, title="Write docs", project="Docs", pid=3),
        piece(4, TUE, 12, 20, ticket="abc-9", title="Other", pid=4),
    ]
    sheet = timesheet.build(slices, (MON, TUE), BERLIN, 15)
    assert [(r.label, r.detail) for r in sheet.rows] == [
        ("ABC-9", "Other"),
        ("PROJ-1", "Fix it"),
        ("Docs — Write docs", ""),  # tasks without a ticket come last, as "Area — Title"
    ]
    proj = sheet.rows[1]
    assert proj.seconds == {MON: 70 * 60, TUE: 30 * 60}
    assert proj.minutes == {MON: 75, TUE: 30}
    assert sheet.day_total_min(MON) == 75
    assert sheet.day_total_min(TUE) == 30 + 45 + 15
    assert sheet.total_min == 75 + 30 + 45 + 15
    assert sheet.raw_seconds == (70 + 30 + 40 + 20) * 60
    assert sheet.raw_min == 160
    assert not sheet.empty


def test_time_across_midnight_is_split_between_the_local_days():
    start = at(MON, 23, 30)
    slices = [TimeSlice(1, "A-1", "Late", 2, "PMS", start, start + timedelta(hours=1))]
    sheet = timesheet.build(slices, (MON, TUE), BERLIN, 0)
    (row,) = sheet.rows
    assert row.minutes == {MON: 30, TUE: 30}


def test_a_small_cell_never_disappears_and_totals_are_sums_of_rounded_cells():
    slices = [piece(i, TUE, 9 + i, 4, ticket=f"T-{i}") for i in range(4)]
    sheet = timesheet.build(slices, (TUE,), BERLIN, 15)
    assert [r.minutes[TUE] for r in sheet.rows] == [15, 15, 15, 15]
    assert sheet.total_min == 60  # not the 16 real minutes
    assert sheet.raw_min == 16


def test_time_outside_the_days_is_ignored_and_an_empty_sheet_is_empty():
    sheet = timesheet.build([piece(1, date(2026, 10, 1), 9, 60)], (TUE,), BERLIN, 15)
    assert sheet.empty
    assert sheet.total_min == 0


def test_text_and_csv_for_a_day_and_a_week():
    slices = [
        piece(1, MON, 9, 70),
        piece(3, TUE, 10, 40, ticket=None, title="Docs, v2", project="Area"),
    ]
    day = timesheet.build(slices, (MON,), BERLIN, 15)
    assert timesheet.to_text(day) == "Mon 2 Nov 2026\nPROJ-1\t1h 15m\nTotal\t1h 15m\n"
    week = timesheet.build(slices, WEEK, BERLIN, 15)
    lines = timesheet.to_text(week).splitlines()
    assert (
        lines[0]
        == "\tMon 2 Nov\tTue 3 Nov\tWed 4 Nov\tThu 5 Nov\tFri 6 Nov\tSat 7 Nov\tSun 8 Nov\tTotal"
    )
    assert lines[1] == "PROJ-1\t1h 15m\t\t\t\t\t\t\t1h 15m"
    assert lines[2].startswith("Area — Docs, v2\t\t45m")
    assert lines[3].endswith("2h 00m")
    table = list(csv.reader(io.StringIO(timesheet.to_csv(week))))
    assert table[0][:3] == ["Ticket or task", "Title", "2026-11-02"]
    assert table[1][:4] == ["PROJ-1", "Fix it", "1.25", ""]
    assert table[2][0] == "Area — Docs, v2"  # the comma survives CSV quoting
    assert table[-1][0] == "Total"
    assert table[-1][-1] == "2.00"


def test_week_days_and_title():
    assert WEEK[0] == MON
    assert WEEK[-1] == date(2026, 11, 8)
    assert timesheet.week_days(date(2026, 11, 8)) == WEEK  # Sunday belongs to its week
    assert timesheet.week_title(WEEK) == "2 Nov – 8 Nov"


# --- Standup --------------------------------------------------------------------------------


def test_the_standup_in_text_and_markdown():
    result = standup.build(
        [
            standup.Worked("PROJ-2 API tests", 7200, False),
            standup.Worked("PROJ-1 Fix login", 5400, True, "Rahul"),
            standup.Worked("Quick call", 20, False),
        ],
        [standup.Planned("PROJ-2 API tests", 120), standup.Planned("Demo", None, "Priya")],
        [standup.Blocker("PROJ-9 Deploy", "Waiting for approval or review", 3, "QA sign-off")],
        last_day=MON,
        today=TUE,
    )
    assert result.yesterday == (
        "PROJ-1 Fix login (for Rahul) — done, 1h 30m",  # finished work first
        "PROJ-2 API tests — 2h 00m",
        "Quick call",  # under a minute: no time shown
    )
    assert result.plan == ("PROJ-2 API tests — 2h 00m", "Demo (for Priya) — rest of day")
    assert result.blockers == (
        "PROJ-9 Deploy — waiting for approval or review, waiting 3d (QA sign-off)",
    )
    text = standup.to_text(result)
    assert text.startswith("Yesterday:\n- PROJ-1 Fix login (for Rahul) — done, 1h 30m\n")
    assert "\nToday:\n- PROJ-2" in text
    assert text.endswith("(QA sign-off)\n")
    md = standup.to_markdown(result)
    assert md.startswith("**Yesterday**\n- PROJ-1")
    assert "**Blockers**\n- PROJ-9" in md


def test_an_empty_standup_says_so_and_names_the_day_after_a_weekend():
    friday = date(2026, 10, 30)
    result = standup.build([], [], [], last_day=friday, today=MON)
    assert result.sections() == (
        ("Yesterday (Fri 30 Oct 2026)", ("Nothing",)),
        ("Today", ("Nothing",)),
        ("Blockers", ("None",)),
    )
    assert standup.build([], [], [], last_day=MON, today=TUE).yesterday_heading == "Yesterday"
    blocker = standup.build([], [], [standup.Blocker("X", "Blocked", 0)], last_day=MON, today=TUE)
    assert blocker.blockers == ("X — blocked, since today",)


# --- Weekly report --------------------------------------------------------------------------


def occurrence(start, minutes, adhoc=False, open_ended=False):
    return Occurrence(1, start, start, minutes, "m", None, (), None, False, adhoc, open_ended)


def test_the_weekly_report():
    slices = [
        piece(1, MON, 9, 120),
        piece(2, MON, 12, 60, ticket=None, title="Docs", project="Docs", pid=3),
        piece(1, TUE, 9, 60),
        piece(5, WEEK[0] - timedelta(days=3), 9, 60),  # last week: ignored
    ]
    plan = [
        PlannedItem(MON, 1, "PROJ-1 Fix it", 90, False),
        PlannedItem(TUE, 1, "PROJ-1 Fix it", 30, True),  # planned 120 in total
        PlannedItem(TUE, 2, "Docs", None, True),  # rest of day, carried over from Monday
        PlannedItem(TUE, 7, "Never started", 45, False),
    ]
    report = reports.build(WEEK, BERLIN, slices, plan, completed=4, meetings=(95, 20))
    assert [(p.name, p.seconds // 60) for p in report.per_project] == [("PMS", 180), ("Docs", 60)]
    assert report.per_day_s[:2] == (180 * 60, 60 * 60)
    assert sum(report.per_day_s[2:]) == 0
    assert report.tracked_s == 240 * 60
    assert report.focus_min == 240
    assert (report.meeting_min, report.adhoc_min, report.completed) == (95, 20, 4)
    assert (report.carried, report.planned_tasks) == (2, 3)  # items carried; distinct tasks
    by_label = {t.label: t for t in report.tasks}
    first = by_label["PROJ-1 Fix it"]
    assert (first.planned_min, first.actual_min, first.over_min) == (120, 180, 60)
    docs = by_label["Docs"]
    assert docs.rest_of_day
    assert docs.actual_min == 60
    never = by_label["Never started"]
    assert (never.planned_min, never.actual_min) == (45, 0)
    assert report.tasks[0] is first  # most time first
    assert not report.empty


def test_an_empty_week_is_empty():
    report = reports.build(WEEK, BERLIN, [], [], completed=0, meetings=(0, 0))
    assert report.empty
    assert report.tasks == () == report.per_project


def test_meeting_minutes_split_ad_hoc_and_stop_at_now():
    now = at(TUE, 11)
    lo, hi = at(TUE, 0), at(TUE, 23)
    occurrences = [
        occurrence(at(TUE, 9), 30),  # scheduled, 30
        occurrence(at(TUE, 10, 45), 60),  # still running: only until now (15)
        occurrence(at(TUE, 8), 20, adhoc=True),
        occurrence(at(TUE, 14), 60),  # later today: not yet
        occurrence(at(MON, 9), 60),  # yesterday: outside the range
    ]
    assert reports.meeting_minutes(occurrences, lo, hi, now) == (45, 20)
    open_one = occurrence(at(TUE, 10), 30, adhoc=True, open_ended=True)
    assert reports.meeting_minutes([open_one], lo, hi, now)[1] == 60


def test_overlapping_meetings_are_counted_once():
    now = at(TUE, 17)
    lo, hi = at(TUE, 0), at(TUE, 23)
    overlapping = [
        occurrence(at(TUE, 9), 60),  # 09:00-10:00
        occurrence(at(TUE, 9, 30), 60),  # 09:30-10:30: only 30 more minutes
        occurrence(at(TUE, 10), 30),  # inside the union already
        occurrence(at(TUE, 13), 30),
    ]
    assert reports.meeting_minutes(overlapping, lo, hi, now) == (90 + 30, 0)
