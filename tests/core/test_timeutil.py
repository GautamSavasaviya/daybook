from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from daybook.core import timeutil

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")


def test_to_db_converts_to_utc_and_drops_microseconds():
    instant = datetime(2026, 3, 1, 10, 5, 7, 999_999, tzinfo=BERLIN)
    assert timeutil.to_db(instant) == "2026-03-01T09:05:07Z"


def test_to_db_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="naive"):
        timeutil.to_db(datetime(2026, 1, 1, 12, 0))


def test_from_db_round_trip():
    instant = datetime(2026, 11, 3, 13, 5, tzinfo=UTC)
    assert timeutil.from_db(timeutil.to_db(instant)) == instant
    assert timeutil.from_db("2026-11-03T13:05:00Z").tzinfo is UTC


@pytest.mark.parametrize(
    "text", ["2026-11-03 13:05:00", "2026-11-03T13:05:00+00:00", "2026-11-03T13:05Z", ""]
)
def test_from_db_rejects_other_formats(text):
    with pytest.raises(ValueError, match="stored instant"):
        timeutil.from_db(text)


def test_stored_strings_sort_chronologically():
    instants = [
        datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=5))),
        datetime(2025, 12, 31, 23, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 3, 59, 59, tzinfo=UTC),
    ]
    assert sorted(timeutil.to_db(i) for i in instants) == [
        timeutil.to_db(i) for i in sorted(instants)
    ]


def test_dates_round_trip_and_validate():
    assert timeutil.date_to_db(date(2026, 2, 28)) == "2026-02-28"
    assert timeutil.date_from_db("2026-02-28") == date(2026, 2, 28)
    with pytest.raises(ValueError, match="stored date"):
        timeutil.date_from_db("28/02/2026")


def test_localize_regular_time():
    result = timeutil.localize(datetime(2026, 6, 1, 9, 30), BERLIN)
    assert result.utcoffset() == timedelta(hours=2)
    assert result.hour == 9


def test_localize_nonexistent_time_moves_forward_by_the_gap():
    # 29 Mar 2026: Berlin clocks jump from 02:00 to 03:00.
    result = timeutil.localize(datetime(2026, 3, 29, 2, 30), BERLIN)
    assert (result.hour, result.minute) == (3, 30)
    assert result.utcoffset() == timedelta(hours=2)


def test_localize_ambiguous_time_takes_first_occurrence():
    # 25 Oct 2026: Berlin repeats 02:00-03:00; the first one is still CEST (+2).
    result = timeutil.localize(datetime(2026, 10, 25, 2, 30), BERLIN)
    assert result.utcoffset() == timedelta(hours=2)


def test_localize_rejects_aware_input():
    with pytest.raises(ValueError, match="naive"):
        timeutil.localize(datetime(2026, 1, 1, tzinfo=UTC), BERLIN)


@pytest.mark.parametrize(
    ("day", "tz", "hours"),
    [
        (date(2026, 6, 1), BERLIN, 24),
        (date(2026, 3, 29), BERLIN, 23),
        (date(2026, 10, 25), BERLIN, 25),
        (date(2026, 3, 8), NEW_YORK, 23),
        (date(2026, 11, 1), NEW_YORK, 25),
    ],
)
def test_local_day_bounds_cover_dst_days(day, tz, hours):
    start, end = timeutil.local_day_bounds(day, tz)
    assert start.tzinfo is UTC
    assert end - start == timedelta(hours=hours)
    assert timeutil.local_date(start, tz) == day
    assert timeutil.local_date(end - timedelta(seconds=1), tz) == day


def test_local_date_uses_the_local_calendar():
    late_utc = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
    assert timeutil.local_date(late_utc, BERLIN) == date(2026, 11, 4)


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(0, "0m"), (45, "45m"), (60, "1h 00m"), (65, "1h 05m"), (450, "7h 30m"), (-15, "-15m")],
)
def test_fmt_duration(minutes, text):
    assert timeutil.fmt_duration(minutes) == text


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=20), "now"),
        (timedelta(seconds=-20), "now"),
        (timedelta(minutes=12), "in 12 min"),
        (timedelta(minutes=-12), "12 min ago"),
        (timedelta(minutes=65), "in 1h 05m"),
        (timedelta(minutes=-90), "1h 30m ago"),
    ],
)
def test_fmt_relative(delta, text):
    assert timeutil.fmt_relative(delta) == text


@pytest.mark.parametrize(
    ("hour", "minute", "h24", "h12"),
    [(14, 5, "14:05", "2:05 PM"), (0, 0, "00:00", "12:00 AM"), (12, 30, "12:30", "12:30 PM")],
)
def test_fmt_clock(hour, minute, h24, h12):
    local = datetime(2026, 6, 1, hour, minute, tzinfo=BERLIN)
    instant = local.astimezone(UTC)
    assert timeutil.fmt_clock(instant, BERLIN, use_24h=True) == h24
    assert timeutil.fmt_clock(instant, BERLIN, use_24h=False) == h12
