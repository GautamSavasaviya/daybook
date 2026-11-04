"""Auto-schedule (PLAN §5.1): the mandatory cases and the extra ones listed there."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from daybook.core.calendar import Interval, WorkCalendar
from daybook.core.models import TimeOffPart
from daybook.core.planner import Busy, PlanInput, SegmentKind, schedule

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 11, 3)  # Tuesday; Berlin is UTC+1
CAL = WorkCalendar(frozenset(range(5)))  # 09:00-18:00, lunch 13:00-14:00


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def meeting(start: datetime, minutes: int, key: str = "m") -> Busy:
    return Busy(start, start + timedelta(minutes=minutes), key, key)


def run(items, meetings=(), now=None, cal=CAL, day=DAY):
    return schedule(
        cal.day_window(day, BERLIN),
        cal.lunch(day, BERLIN),
        list(meetings),
        list(items),
        now or at(8, 0, day),
    )


def tasks(result):
    return [
        (s.plan_item_id, s.start.astimezone(BERLIN).strftime("%H:%M"), s.minutes, s.part)
        for s in result.task_segments()
    ]


def test_a_simple_day_fills_in_order_and_splits_around_lunch():
    result = run([PlanInput(1, 10, 180), PlanInput(2, 20, 120)])
    assert tasks(result) == [(1, "09:00", 180, 1), (2, "12:00", 60, 1), (2, "14:00", 60, 2)]
    assert result.overflow == ()
    assert result.capacity.free_min == 8 * 60
    assert result.capacity.planned_min == 300
    lunch = [s for s in result.segments if s.kind == SegmentKind.LUNCH]
    assert len(lunch) == 1


def test_planning_starts_mid_day():
    result = run([PlanInput(1, 10, 60)], now=at(10, 41) + timedelta(seconds=30))
    assert tasks(result) == [(1, "10:41", 60, 1)]
    assert result.free_windows[0].start == at(10, 41) + timedelta(seconds=30)
    # 10:41:30 to 13:00 is 138 whole minutes, then 14:00 to 18:00.
    assert result.capacity.free_min == 138 + 240


def test_a_meeting_overlapping_lunch_merges_into_one_busy_block():
    result = run([PlanInput(1, 10, 300)], [meeting(at(12, 30), 60, "standup")])
    assert [(w.start, w.end) for w in result.free_windows] == [
        (at(9), at(12, 30)),
        (at(14), at(18)),
    ]
    assert tasks(result) == [(1, "09:00", 210, 1), (1, "14:00", 90, 2)]
    kinds = [s.kind for s in result.segments if s.kind != SegmentKind.TASK]
    assert kinds == [SegmentKind.MEETING, SegmentKind.LUNCH]  # both still shown


def test_two_meetings_back_to_back():
    result = run(
        [PlanInput(1, 10, 90)],
        [meeting(at(9, 30), 30, "a"), meeting(at(10), 30, "b")],
    )
    assert [(w.start, w.end) for w in result.free_windows][:2] == [
        (at(9), at(9, 30)),
        (at(10, 30), at(13)),
    ]
    assert tasks(result) == [(1, "09:00", 30, 1), (1, "10:30", 60, 2)]
    assert [s.window for s in result.task_segments()] == [0, 1]


def test_the_plan_overflows_past_the_end_of_the_day():
    result = run([PlanInput(1, 10, 300), PlanInput(2, 20, 200), PlanInput(3, 30, 30)])
    assert [(o.plan_item_id, o.minutes) for o in result.overflow] == [(2, 20), (3, 30)]
    assert result.capacity.over_by == 50
    last = result.task_segments()[-1]
    assert last.end == at(18)  # nothing past the window end


def test_not_a_working_day_overflows_everything():
    saturday = date(2026, 11, 7)
    result = run([PlanInput(1, 10, 30), PlanInput(2, 20, None)], day=saturday)
    assert not result.working
    assert result.task_segments() == ()
    assert [(o.plan_item_id, o.minutes) for o in result.overflow] == [(1, 30), (2, None)]


def test_full_day_leave_is_not_working_and_half_days_shrink_the_window():
    off = WorkCalendar(frozenset(range(5)), {DAY: TimeOffPart.FULL})
    assert not run([PlanInput(1, 10, 30)], cal=off).working
    morning_off = WorkCalendar(frozenset(range(5)), {DAY: TimeOffPart.AM})
    result = run([PlanInput(1, 10, 60)], cal=morning_off)
    assert tasks(result) == [(1, "14:00", 60, 1)]
    assert result.capacity.free_min == 240
    afternoon_off = WorkCalendar(frozenset(range(5)), {DAY: TimeOffPart.PM})
    result = run([PlanInput(1, 10, 300)], cal=afternoon_off)
    assert result.capacity.free_min == 240
    assert [(o.plan_item_id, o.minutes) for o in result.overflow] == [(1, 60)]


def test_now_after_the_window_end_overflows_all_timed_work():
    result = run([PlanInput(1, 10, 30), PlanInput(2, 20, None)], now=at(19))
    assert result.free_windows == ()
    assert [(o.plan_item_id, o.minutes) for o in result.overflow] == [(1, 30)]
    assert result.rest_of_day_min == {2: 0}


def test_zero_items_and_a_meeting_outside_working_hours():
    result = run([], [meeting(at(7), 30, "early")])
    assert result.task_segments() == ()
    assert result.capacity.free_min == 480  # the 07:00 meeting doesn't use work time
    assert any(s.kind == SegmentKind.MEETING for s in result.segments)


def test_sub_five_minute_gaps_are_not_filled():
    result = run(
        [PlanInput(1, 10, 30)],
        [meeting(at(9), 57, "a"), meeting(at(10, 1), 59, "b")],
    )
    # 09:57-10:01 is a 4-minute gap: skipped.
    assert tasks(result) == [(1, "11:00", 30, 1)]


def test_an_item_with_nothing_remaining_takes_no_time():
    result = run([PlanInput(1, 10, 60, tracked_min=75), PlanInput(2, 20, 30)])
    assert tasks(result) == [(2, "09:00", 30, 1)]
    assert result.overflow == ()
    assert PlanInput(1, 10, 60, tracked_min=75).remaining_min == 0


def test_rest_of_day_items_share_the_leftover_time():
    result = run([PlanInput(1, 10, 120), PlanInput(2, 20, None), PlanInput(3, 30, None)])
    assert result.rest_of_day_min == {2: 180, 3: 180}
    assert result.capacity.flexible_count == 2
    assert result.overflow == ()
    odd = run([PlanInput(1, 10, 121), PlanInput(2, 20, None), PlanInput(3, 30, None)])
    assert odd.rest_of_day_min == {2: 180, 3: 179}  # the spare minute goes to the earliest


def test_one_rest_of_day_item_fills_the_day():
    result = run([PlanInput(1, 10, None)])
    assert result.rest_of_day_min == {1: 480}
    assert [s.minutes for s in result.task_segments()] == [240, 240]


def test_rest_of_day_with_overflowing_timed_items_gets_nothing_and_never_overflows():
    result = run([PlanInput(1, 10, 600), PlanInput(2, 20, None)])
    assert result.rest_of_day_min == {2: 0}
    assert [(o.plan_item_id, o.minutes) for o in result.overflow] == [(1, 120)]


def test_an_open_adhoc_meeting_keeps_pushing_the_plan():
    # The caller passes an open meeting with end = max(start + length, now + 1 min).
    now = at(10, 50)
    adhoc = Busy(at(10), now + timedelta(minutes=1), "adhoc", "Meeting")
    result = run([PlanInput(1, 10, 60)], [adhoc], now=now)
    assert tasks(result) == [(1, "10:51", 60, 1)]


def test_dst_change_day_has_the_right_length():
    # 25 Oct 2026: Berlin falls back at 03:00, but working hours are unaffected.
    sunday_cal = WorkCalendar(frozenset(range(7)))
    day = date(2026, 10, 25)
    result = run([PlanInput(1, 10, 30)], cal=sunday_cal, day=day, now=at(0, 0, day))
    assert result.capacity.free_min == 480
    assert result.task_segments()[0].start == at(9, 0, day)


def test_schedule_lookups():
    result = run([PlanInput(1, 10, 60), PlanInput(2, 20, 60)])
    assert result.segment_at(at(9, 30)).plan_item_id == 1
    assert result.segment_at(at(13, 30)) is None
    assert result.next_task_after(at(9, 30), 1).plan_item_id == 2
    assert result.next_task_after(at(17), 2) is None
    assert result.overflowing_ids() == frozenset()


def test_deterministic():
    items = [PlanInput(1, 10, 45), PlanInput(2, 20, None)]
    assert run(items, [meeting(at(10), 30)]) == run(items, [meeting(at(10), 30)])


def test_interval_helpers():
    span = Interval(at(9), at(10))
    assert span.minutes == 60
    assert span.contains(at(9))
    assert not span.contains(at(10))
