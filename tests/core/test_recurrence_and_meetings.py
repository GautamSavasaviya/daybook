"""Recurrence (PLAN §5.4 mandatory cases), meeting occurrences and the working window."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import meetings, recurrence
from daybook.core.calendar import WorkCalendar
from daybook.core.errors import ValidationError
from daybook.core.models import (
    Meeting,
    MeetingDraft,
    MeetingException,
    OccurrenceEdit,
    Recurrence,
    RecurrenceFreq,
    TimeOffPart,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

DAILY = Recurrence(RecurrenceFreq.DAILY)
WEEKDAYS = Recurrence(RecurrenceFreq.WEEKDAYS)
MONTHLY = Recurrence(RecurrenceFreq.MONTHLY)


def local(tz, *args):
    return datetime(*args, tzinfo=tz).astimezone(UTC)


def local_times(starts, tz):
    return [s.astimezone(tz).strftime("%Y-%m-%d %H:%M") for s in starts]


def window(tz, first, last):
    return local(tz, *first), local(tz, *last)


# --- Recurrence -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tz", "spring", "autumn"),
    [
        (BERLIN, (2027, 3, 26), (2026, 10, 23)),  # changes 28 Mar / 25 Oct
        (NEW_YORK, (2027, 3, 12), (2026, 10, 30)),  # changes 14 Mar / 1 Nov
    ],
)
def test_daily_meetings_keep_their_wall_clock_time_across_dst(tz, spring, autumn):
    for first in (spring, autumn):
        anchor = local(tz, *first, 10, 0)
        start, end = window(tz, (*first, 0, 0), (*_plus_days(first, 4), 0, 0))
        result = recurrence.starts(DAILY, anchor, tz, start, end)
        assert [s.astimezone(tz).hour for s in result] == [10, 10, 10, 10]
        offsets = {s.astimezone(tz).utcoffset() for s in result}
        assert len(offsets) == 2  # the UTC time moved by an hour; the local one didn't


def _plus_days(parts, days):
    moved = date(*parts) + timedelta(days=days)
    return moved.year, moved.month, moved.day


def test_a_time_that_does_not_exist_moves_forward_and_a_repeated_one_takes_the_first():
    gap = local(BERLIN, 2027, 3, 27, 2, 30)  # the day before the gap
    start, end = window(BERLIN, (2027, 3, 27, 0, 0), (2027, 3, 29, 0, 0))
    result = recurrence.starts(DAILY, gap, BERLIN, start, end)
    assert local_times(result, BERLIN) == ["2027-03-27 02:30", "2027-03-28 03:30"]
    fold = local(BERLIN, 2026, 10, 24, 2, 30)
    start, end = window(BERLIN, (2026, 10, 24, 0, 0), (2026, 10, 26, 0, 0))
    second = recurrence.starts(DAILY, fold, BERLIN, start, end)[1]
    assert second.astimezone(BERLIN).utcoffset() == timedelta(hours=2)  # the first 02:30


def test_monthly_on_the_31st_falls_on_the_last_day_of_shorter_months():
    anchor = local(BERLIN, 2027, 1, 31, 9, 0)
    start, end = window(BERLIN, (2027, 1, 1, 0, 0), (2027, 6, 1, 0, 0))
    days = [
        s.astimezone(BERLIN).date().isoformat()
        for s in recurrence.starts(MONTHLY, anchor, BERLIN, start, end)
    ]
    assert days == ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31"]


def test_monthly_from_february_29_and_every_two_months():
    anchor = date(2028, 2, 29)
    yearly_ish = Recurrence(RecurrenceFreq.MONTHLY, interval=12)
    found = list(recurrence.dates(yearly_ish, anchor, date(2028, 1, 1), date(2033, 1, 1)))
    assert found == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
        date(2031, 2, 28),
        date(2032, 2, 29),
    ]
    bimonthly = Recurrence(RecurrenceFreq.MONTHLY, interval=2)
    found = list(
        recurrence.dates(bimonthly, date(2026, 11, 15), date(2027, 3, 1), date(2027, 8, 1))
    )
    assert found == [date(2027, 3, 15), date(2027, 5, 15), date(2027, 7, 15)]


def test_weekly_on_chosen_days_and_every_other_week():
    rule = Recurrence(RecurrenceFreq.WEEKLY, days=frozenset({0, 2}))  # Mon, Wed
    found = list(recurrence.dates(rule, date(2026, 11, 2), date(2026, 11, 1), date(2026, 11, 15)))
    assert found == [date(2026, 11, 2), date(2026, 11, 4), date(2026, 11, 9), date(2026, 11, 11)]
    fortnightly = Recurrence(RecurrenceFreq.WEEKLY, interval=2)  # anchor's weekday
    found = list(
        recurrence.dates(fortnightly, date(2026, 11, 3), date(2026, 11, 1), date(2026, 12, 1))
    )
    assert found == [date(2026, 11, 3), date(2026, 11, 17), date(2026, 12, 1)]


def test_weekdays_daily_interval_and_until():
    found = list(
        recurrence.dates(WEEKDAYS, date(2026, 11, 6), date(2026, 11, 1), date(2026, 11, 10))
    )
    assert found == [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)]
    every_third = Recurrence(RecurrenceFreq.DAILY, interval=3, until=date(2026, 11, 12))
    found = list(
        recurrence.dates(every_third, date(2026, 11, 3), date(2026, 11, 4), date(2026, 12, 1))
    )
    assert found == [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 12)]
    assert (
        list(recurrence.dates(DAILY, date(2026, 11, 3), date(2026, 11, 5), date(2026, 11, 4))) == []
    )


def test_one_off_and_window_edges():
    anchor = local(BERLIN, 2026, 11, 3, 10, 0)
    assert recurrence.starts(None, anchor, BERLIN, anchor, anchor + timedelta(minutes=1)) == [
        anchor
    ]
    assert (
        recurrence.starts(
            None, anchor, BERLIN, anchor + timedelta(seconds=1), anchor + timedelta(hours=1)
        )
        == []
    )
    # Nothing before the anchor, even when the window starts earlier.
    start, end = window(BERLIN, (2026, 11, 1, 0, 0), (2026, 11, 4, 0, 0))
    assert local_times(recurrence.starts(DAILY, anchor, BERLIN, start, end), BERLIN) == [
        "2026-11-03 10:00"
    ]


def test_next_date_for_recurring_tasks():
    assert recurrence.next_date(WEEKDAYS, date(2026, 11, 6), date(2026, 11, 6)) == date(2026, 11, 9)
    assert recurrence.next_date(MONTHLY, date(2027, 1, 31), date(2027, 1, 31)) == date(2027, 2, 28)
    ending = Recurrence(RecurrenceFreq.DAILY, until=date(2026, 11, 3))
    assert recurrence.next_date(ending, date(2026, 11, 1), date(2026, 11, 3)) is None


def test_json_round_trip_and_validation():
    rule = Recurrence(RecurrenceFreq.WEEKLY, 2, frozenset({4, 0}), date(2027, 6, 30))
    assert recurrence.from_json(recurrence.to_json(rule)) == rule
    assert recurrence.from_json(None) is None
    assert recurrence.from_json(recurrence.to_json(WEEKDAYS)) == WEEKDAYS
    # Days only matter for weekly rules; weekdays always repeat every week.
    assert (
        recurrence.validate(Recurrence(RecurrenceFreq.DAILY, days=frozenset({1}))).days
        == frozenset()
    )
    assert recurrence.validate(Recurrence(RecurrenceFreq.WEEKDAYS, interval=3)).interval == 1
    for bad in ("[1]", '{"freq": "yearly"}', '{"freq": "daily", "interval": "x"}'):
        with pytest.raises((ValueError, KeyError)):
            recurrence.from_json(bad)
    with pytest.raises(ValidationError):
        recurrence.validate(Recurrence(RecurrenceFreq.DAILY, interval=0))
    with pytest.raises(ValidationError):
        recurrence.validate(Recurrence(RecurrenceFreq.WEEKLY, days=frozenset({7})))


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        (None, "Doesn't repeat"),
        (DAILY, "Daily"),
        (Recurrence(RecurrenceFreq.DAILY, 2), "Every 2 days"),
        (WEEKDAYS, "Weekdays"),
        (Recurrence(RecurrenceFreq.WEEKLY), "Weekly on Tue"),
        (Recurrence(RecurrenceFreq.WEEKLY, 2, frozenset({0, 2})), "Every 2 weeks on Mon, Wed"),
        (MONTHLY, "Monthly on day 3"),
        (
            Recurrence(RecurrenceFreq.MONTHLY, 3, until=date(2027, 6, 30)),
            "Every 3 months on day 3 until 30 Jun",
        ),
    ],
)
def test_describe(rule, text):
    assert recurrence.describe(rule, date(2026, 11, 3)) == text


# --- Meetings ---------------------------------------------------------------------------


def series(rule=WEEKDAYS, start=None, **changes) -> Meeting:
    base = {
        "id": 7,
        "title": "Standup",
        "start": start or local(BERLIN, 2026, 11, 2, 10, 0),
        "duration_min": 15,
        "tzid": "Europe/Berlin",
        "link": "https://meet.example.com/x",
        "recurrence": rule,
        "reminder_offsets": (10, 1),
        "project_id": None,
        "is_adhoc": False,
        "open_ended": False,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(changes)
    return Meeting(**base)


def week():
    return window(BERLIN, (2026, 11, 2, 0, 0), (2026, 11, 9, 0, 0))


def test_occurrences_of_a_series():
    result = meetings.occurrences(series(), [], *week())
    assert len(result) == 5
    first = result[0]
    assert (first.title, first.duration_min, first.recurring, first.edited) == (
        "Standup",
        15,
        True,
        False,
    )
    assert first.end - first.start == timedelta(minutes=15)
    assert first.key == "7:2026-11-02T09:00:00Z"


def test_cancelled_and_moved_single_occurrences():
    tuesday = local(BERLIN, 2026, 11, 3, 10, 0)
    wednesday = local(BERLIN, 2026, 11, 4, 10, 0)
    changes = [
        MeetingException(1, 7, tuesday, cancelled=True),
        MeetingException(
            2,
            7,
            wednesday,
            title="Long standup",
            start=wednesday + timedelta(hours=4),
            duration_min=45,
            link="",
            reminder_offsets=(5,),
        ),
    ]
    result = meetings.occurrences(series(), changes, *week())
    assert len(result) == 4
    moved = next(o for o in result if o.edited)
    assert (moved.title, moved.duration_min, moved.link, moved.reminder_offsets) == (
        "Long standup",
        45,
        None,  # cleared: no link on this day
        (5,),
    )
    assert moved.start.astimezone(BERLIN).hour == 14
    assert moved.original_start == wednesday


def test_a_moved_occurrence_appears_in_the_window_it_moved_to():
    friday = local(BERLIN, 2026, 11, 6, 10, 0)
    to_monday = MeetingException(3, 7, friday, start=local(BERLIN, 2026, 11, 9, 9, 0))
    next_week = window(BERLIN, (2026, 11, 9, 0, 0), (2026, 11, 10, 0, 0))
    result = meetings.occurrences(series(), [to_monday], *next_week)
    assert [o.original_start for o in result] == [friday, local(BERLIN, 2026, 11, 9, 10, 0)]
    this_week = meetings.occurrences(series(), [to_monday], *week())
    assert friday not in [o.start for o in this_week]
    # An exception for a time the series doesn't have is ignored.
    bogus = MeetingException(
        4, 7, local(BERLIN, 2026, 11, 7, 10, 0), start=local(BERLIN, 2026, 11, 9, 8, 0)
    )
    assert len(meetings.occurrences(series(), [bogus], *next_week)) == 1


def test_series_edits_report_orphaned_changes():
    tuesday = local(BERLIN, 2026, 11, 3, 10, 0)
    meeting = series()
    changes = [MeetingException(1, 7, tuesday, cancelled=True)]
    assert meetings.orphaned_exceptions(meeting.start, meeting.recurrence, meeting, changes) == []
    later = meeting.start + timedelta(hours=1)
    assert meetings.orphaned_exceptions(later, meeting.recurrence, meeting, changes) == changes
    mondays = Recurrence(RecurrenceFreq.WEEKLY, days=frozenset({0}))
    assert meetings.orphaned_exceptions(meeting.start, mondays, meeting, changes) == changes


def test_unknown_zone_falls_back():
    meeting = series(tzid="Nowhere/Land")
    result = meetings.occurrences(meeting, [], *week(), BERLIN)
    assert result[0].start.astimezone(BERLIN).hour == 10
    assert meetings.zone("Nowhere/Land") is UTC


def test_adhoc_meeting_helpers():
    start = NOW - timedelta(minutes=50)
    occurrence = meetings.occurrences(
        series(None, start=start, is_adhoc=True, open_ended=True, duration_min=30),
        [],
        start,
        start + timedelta(seconds=1),
    )[0]
    assert meetings.effective_end(occurrence, NOW) == NOW + timedelta(minutes=1)
    assert meetings.in_progress(occurrence, NOW)
    closed = meetings.occurrences(
        series(None, start=start, is_adhoc=True, duration_min=30),
        [],
        start,
        start + timedelta(seconds=1),
    )[0]
    assert meetings.effective_end(closed, NOW) == start + timedelta(minutes=30)
    assert not meetings.in_progress(closed, NOW)
    assert meetings.ended_duration(start, NOW) == 50
    assert meetings.ended_duration(NOW, NOW + timedelta(seconds=5)) == 1
    assert meetings.ended_duration(start, NOW + timedelta(seconds=59)) == 50


def test_validate_draft_and_edit():
    draft = MeetingDraft(
        " Sync ",
        local(BERLIN, 2026, 11, 3, 10, 0) + timedelta(seconds=40),
        30,
        " zoommtg://x ",
        WEEKDAYS,
        (1, 10, 10),
    )
    clean = meetings.validate_draft(draft)
    assert (clean.title, clean.link, clean.reminder_offsets) == ("Sync", "zoommtg://x", (10, 1))
    assert clean.start.second == 0
    with pytest.raises(ValueError, match="aware"):
        meetings.validate_draft(MeetingDraft("x", datetime(2026, 11, 3, 10), 30))
    with pytest.raises(ValidationError):
        meetings.validate_draft(MeetingDraft("x", NOW, 0))
    edit = meetings.validate_edit(OccurrenceEdit(" a ", NOW, 15, None, (5,)))
    assert edit.title == "a"


# --- Working window ---------------------------------------------------------------------


def test_day_window_lunch_and_leave():
    tuesday = date(2026, 11, 3)
    cal = WorkCalendar(
        frozenset(range(5)),
        {
            tuesday: TimeOffPart.AM,
            date(2026, 11, 4): TimeOffPart.PM,
            date(2026, 11, 5): TimeOffPart.FULL,
        },
    )
    morning_off = cal.day_window(tuesday, BERLIN)
    assert morning_off.start == local(BERLIN, 2026, 11, 3, 14, 0)
    assert cal.lunch(tuesday, BERLIN) is None  # lunch is in the half taken off
    assert cal.leave(tuesday, BERLIN).end == local(BERLIN, 2026, 11, 3, 13, 0)
    afternoon_off = cal.day_window(date(2026, 11, 4), BERLIN)
    assert afternoon_off.end == local(BERLIN, 2026, 11, 4, 13, 0)
    assert cal.leave(date(2026, 11, 4), BERLIN).start == local(BERLIN, 2026, 11, 4, 14, 0)
    assert cal.day_window(date(2026, 11, 5), BERLIN) is None
    assert cal.leave(date(2026, 11, 5), BERLIN).minutes == 9 * 60
    assert cal.leave(date(2026, 11, 7), BERLIN) is None  # Saturday
    assert cal.leave(date(2026, 11, 9), BERLIN) is None
    assert cal.lunch(date(2026, 11, 9), BERLIN).minutes == 60


def test_half_day_without_lunch_cuts_at_the_midpoint():
    day = date(2026, 11, 3)
    cal = WorkCalendar(frozenset(range(5)), {day: TimeOffPart.PM}, lunch_minutes=0)
    assert cal.day_window(day, BERLIN).end == local(BERLIN, 2026, 11, 3, 13, 30)
    assert cal.lunch(day, BERLIN) is None


def test_with_time_off_and_is_workday():
    cal = WorkCalendar(frozenset(range(5)), work_start=time(8), work_end=time(16))
    day = date(2026, 11, 3)
    off = cal.with_time_off({day: TimeOffPart.FULL})
    assert not off.is_working(day)
    assert off.is_workday(day)
    assert off.with_time_off({day: None}).is_working(day)
    assert off.day_window(date(2026, 11, 4), BERLIN).minutes == 8 * 60
