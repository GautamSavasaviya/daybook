"""Reminder computation (mandatory, SPEC): every kind and tier, keys, leave, Rest of day,
overflow sets, open ad-hoc meetings, missed reminders, no duplicates, snooze, +15 min."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daybook.core import planner, reminders
from daybook.core.calendar import WorkCalendar
from daybook.core.meetings import effective_end
from daybook.core.models import Occurrence, PauseReason, TimeOffPart, TimerRun, TimerState
from daybook.core.reminders import Action, Kind, PlanLine, Reminder, Snapshot, Tier, View
from daybook.core.settings import Settings

BERLIN = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 11, 3)  # Tuesday
SATURDAY = date(2026, 11, 7)


def at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def meeting(
    *,
    mid=1,
    start=None,
    minutes=15,
    title="Standup",
    link="https://meet/s",
    offsets=(10, 1),
    **extra,
) -> Occurrence:
    start = start or at(10)
    return Occurrence(
        meeting_id=mid,
        original_start=start,
        start=start,
        duration_min=minutes,
        title=title,
        link=link,
        reminder_offsets=offsets,
        project_id=None,
        recurring=False,
        **extra,
    )


def line(
    *, item=1, task=11, allotted=60, tracked_s=0, name="PROJ-123", is_open=True, carried=False
):
    return PlanLine(item, task, name, allotted, tracked_s, is_open, carried)


def running(task=11, since=None) -> TimerState:
    since = since or at(9)
    return TimerState(task, TimerRun.RUNNING, since, since)


def snap(
    now,
    *,
    plan=(),
    occurrences=(),
    timer=None,
    settings=None,
    time_off=None,
    today=TODAY,
    **extra,
) -> Snapshot:
    settings = settings or Settings()
    calendar = WorkCalendar.from_settings(settings, time_off or {})
    busy = [planner.Busy(o.start, effective_end(o, now), o.key, o.title) for o in occurrences]
    inputs = [
        planner.PlanInput(p.item_id, p.task_id, p.allotted_min, p.tracked_s // 60)
        for p in plan
        if p.open
    ]
    if timer is not None and timer.running:
        inputs.sort(key=lambda i: i.task_id != timer.task_id)
    schedule = planner.schedule(
        calendar.day_window(today, BERLIN), calendar.lunch(today, BERLIN), busy, inputs, now
    )
    return Snapshot(
        today=today,
        tz=BERLIN,
        settings=settings,
        calendar=calendar,
        occurrences=tuple(occurrences),
        plan=tuple(plan),
        schedule=schedule,
        timer=timer,
        **extra,
    )


def kinds(result, kind):
    return [r for r in result if r.kind == kind]


def text(reminder, now):
    return reminders.render(reminder, now, BERLIN, True)


# --- Kinds and tiers ------------------------------------------------------------------------


def test_every_kind_declares_its_tier_and_can_be_rendered():
    assert set(reminders.TIERS) == set(Kind)
    assert set(reminders._RENDERERS) == set(Kind)
    assert set(reminders._RELEVANCE) == set(Kind)
    for tier in (Tier.STAY, Tier.ACT, Tier.INFO):
        assert tier in reminders.SOUNDS
    # D-053: which kinds stay on screen.
    stay = {k for k, t in reminders.TIERS.items() if t == Tier.STAY}
    assert stay == {Kind.MEETING, Kind.ALLOT_UP, Kind.OPEN_MEETING, Kind.TEST}
    act = {k for k, t in reminders.TIERS.items() if t == Tier.ACT}
    assert act == {Kind.DRIFT, Kind.MEETING_END, Kind.OVERFLOW}


def test_subjects_group_reminders_per_meeting_task_and_day():
    occurrence = meeting()
    now = at(9, 30)
    result = reminders.compute(snap(now, occurrences=[occurrence]), now)
    subjects = {r.subject for r in kinds(result, Kind.MEETING)}
    assert subjects == {f"meeting:{occurrence.key}"}
    assert Reminder("k", Kind.DRIFT, now, task_id=5).subject == "task:5"
    assert Reminder("k", Kind.SOD, now, day=TODAY).subject == "day:2026-11-03"
    assert Reminder("k", Kind.SUMMARY, now).subject == "summary"
    assert Reminder("k", Kind.TEST, now).subject == "test"


# --- Meetings -------------------------------------------------------------------------------


def test_meeting_reminders_at_each_offset():
    occurrence = meeting()
    now = at(9)
    result = kinds(reminders.compute(snap(now, occurrences=[occurrence]), now), Kind.MEETING)
    assert [(r.key, r.due) for r in result] == [
        (f"meeting:{occurrence.key}:10", at(9, 50)),
        (f"meeting:{occurrence.key}:1", at(9, 59)),
    ]
    message = text(result[0], at(9, 50))
    assert message.title == "Standup in 10 min"
    assert [b.action for b in message.buttons] == [Action.JOIN, Action.SNOOZE]
    assert message.view == View.TODAY
    assert text(result[1], at(10)).title == "Standup starts now"
    assert text(result[1], at(10, 4)).title == "Standup started 4 min ago"


def test_meeting_without_link_has_only_snooze_and_long_titles_are_shortened():
    occurrence = meeting(link=None, title="Quarterly planning with the whole extended team")
    now = at(9)
    (first, _second) = kinds(
        reminders.compute(snap(now, occurrences=[occurrence]), now), Kind.MEETING
    )
    message = text(first, at(9, 50))
    assert len(message.title) <= reminders.TITLE_MAX
    assert message.title.endswith("in 10 min")
    assert "…" in message.title
    assert [b.action for b in message.buttons] == [Action.SNOOZE]


def test_adhoc_meetings_get_no_offset_reminders():
    adhoc = meeting(is_adhoc=True, offsets=())
    now = at(9)
    assert kinds(reminders.compute(snap(now, occurrences=[adhoc]), now), Kind.MEETING) == []


def test_meeting_reminders_are_off_on_leave_unless_asked_for():
    occurrence = meeting()
    off = {TODAY: TimeOffPart.FULL}
    now = at(9)
    assert (
        kinds(
            reminders.compute(snap(now, occurrences=[occurrence], time_off=off), now), Kind.MEETING
        )
        == []
    )
    keep = Settings(meeting_reminders_on_leave=True)
    result = reminders.compute(
        snap(now, occurrences=[occurrence], time_off=off, settings=keep), now
    )
    assert len(kinds(result, Kind.MEETING)) == 2


def test_half_day_leave_suppresses_only_the_off_half():
    morning = meeting(mid=1, start=at(10))
    afternoon = meeting(mid=2, start=at(15), title="Review")
    off = {TODAY: TimeOffPart.AM}
    now = at(8)
    result = kinds(
        reminders.compute(snap(now, occurrences=[morning, afternoon], time_off=off), now),
        Kind.MEETING,
    )
    assert {r.meeting_id for r in result} == {2}


def test_weekend_meetings_still_remind_but_the_day_reminders_do_not():
    saturday = meeting(start=at(11, day=SATURDAY))
    now = at(9, day=SATURDAY)
    result = reminders.compute(snap(now, occurrences=[saturday], today=SATURDAY), now)
    assert len(kinds(result, Kind.MEETING)) == 2
    assert kinds(result, Kind.SOD) == kinds(result, Kind.EOD) == []


# --- Allotment ------------------------------------------------------------------------------


def test_allotment_warning_and_time_up_follow_tracked_time():
    now = at(10)
    plan = [line(allotted=60, tracked_s=30 * 60), line(item=2, task=12, name="PROJ-456")]
    result = reminders.compute(snap(now, plan=plan, timer=running()), now)
    (warn,) = kinds(result, Kind.ALLOT_WARN)
    (up,) = kinds(result, Kind.ALLOT_UP)
    assert warn.key == "allot_warn:2026-11-03:1:60"
    assert warn.due == at(10, 20)
    assert up.key == "allot_up:2026-11-03:1:60"
    assert up.due == at(10, 30)
    assert up.next_task_id == 12

    message = text(up, at(10, 32))
    assert message.title == "Time's up: PROJ-123"
    assert message.body == "Planned 1h 00m, spent 1h 02m. Next: PROJ-456"
    assert [b.action for b in message.buttons] == [Action.SWITCH, Action.EXTEND, Action.DONE]
    assert text(warn, at(10, 20)).title == "PROJ-123: 10 min left"
    assert [b.action for b in text(warn, at(10, 20)).buttons] == [Action.EXTEND]


def test_extending_by_15_min_gives_new_keys():
    now = at(10)
    plan = [line(allotted=60 + reminders.EXTEND_MIN, tracked_s=60 * 60)]
    (up,) = kinds(reminders.compute(snap(now, plan=plan, timer=running()), now), Kind.ALLOT_UP)
    assert up.key == "allot_up:2026-11-03:1:75"
    assert up.due == at(10, 15)
    message = text(up, at(10, 15))
    assert [b.action for b in message.buttons] == [Action.EXTEND, Action.DONE]


def test_no_allotment_reminders_when_paused_for_rest_of_day_or_tiny_allotments():
    now = at(10)
    paused = replace(running(), run=TimerRun.PAUSED, segment_start=None)
    assert (
        kinds(reminders.compute(snap(now, plan=[line()], timer=paused), now), Kind.ALLOT_UP) == []
    )
    rest = [line(allotted=None)]
    result = reminders.compute(snap(now, plan=rest, timer=running()), now)
    assert kinds(result, Kind.ALLOT_UP) == kinds(result, Kind.ALLOT_WARN) == []
    small = [line(allotted=10)]
    result = reminders.compute(snap(now, plan=small, timer=running()), now)
    assert kinds(result, Kind.ALLOT_WARN) == []
    assert len(kinds(result, Kind.ALLOT_UP)) == 1
    other = reminders.compute(snap(now, plan=[line()], timer=running(task=99)), now)
    assert kinds(other, Kind.ALLOT_UP) == []


# --- Drift ----------------------------------------------------------------------------------


def test_drift_nudges_after_five_idle_minutes_in_a_free_window():
    now = at(9, 1)
    plan = [line(allotted=60)]
    (drift,) = kinds(reminders.compute(snap(now, plan=plan), now), Kind.DRIFT)
    assert drift.key == "drift:2026-11-03:1:0"
    assert drift.due == at(9, 5)
    message = text(drift, at(9, 5))
    assert message.title == "Planned now: PROJ-123"
    assert [b.action for b in message.buttons] == [Action.START, Action.SKIP]


def test_drift_counts_from_the_last_timer_activity_and_the_last_meeting():
    plan = [line(allotted=60)]
    later = reminders.compute(snap(at(9, 22), plan=plan, idle_since=at(9, 20)), at(9, 22))
    assert kinds(later, Kind.DRIFT)[0].due == at(9, 25)
    # Idle for longer already (just started, or back from suspend): due now.
    overdue = reminders.compute(snap(at(9, 30), plan=plan, idle_since=at(9, 20)), at(9, 30))
    assert kinds(overdue, Kind.DRIFT)[0].due == at(9, 30)
    standup = meeting(start=at(10))
    after = reminders.compute(snap(at(10, 20), plan=plan, occurrences=[standup]), at(10, 20))
    (drift,) = kinds(after, Kind.DRIFT)
    assert drift.key == "drift:2026-11-03:1:1"  # the second free window of the day
    assert drift.due == at(10, 20)


@pytest.mark.parametrize(
    ("now", "extra"),
    [
        (at(10, 5), {"occurrences": [meeting()]}),  # in a meeting
        (at(8), {}),  # before work
        (at(18, 30), {}),  # after work
        (at(12, 58), {}),  # the window ends (lunch) before the nudge would be due
        (at(9, 30), {"timer": running()}),  # a timer runs
    ],
)
def test_no_drift(now, extra):
    assert kinds(reminders.compute(snap(now, plan=[line()], **extra), now), Kind.DRIFT) == []


def test_no_drift_on_leave_or_without_an_open_item():
    off = {TODAY: TimeOffPart.FULL}
    now = at(9, 1)
    assert kinds(reminders.compute(snap(now, plan=[line()], time_off=off), now), Kind.DRIFT) == []
    assert kinds(reminders.compute(snap(now, plan=[line(is_open=False)]), now), Kind.DRIFT) == []


# --- Back to the task, overflow, open meetings ----------------------------------------------


def test_back_to_the_task_when_the_meeting_that_paused_it_ends():
    standup = meeting()
    paused = TimerState(11, TimerRun.PAUSED, None, at(10), PauseReason.MEETING, standup.key)
    now = at(10, 5)
    s = snap(now, occurrences=[standup], timer=paused, timer_task_name="PROJ-123")
    (back,) = kinds(reminders.compute(s, now), Kind.MEETING_END)
    assert back.key == f"meeting_end:{standup.key}"
    assert back.due == at(10, 15)
    message = text(back, at(10, 15))
    assert message.title == "Back to PROJ-123?"
    assert message.body == "“Standup” has ended."
    assert [b.action for b in message.buttons] == [Action.RESUME, Action.PICK]
    # An open-ended meeting has no end yet.
    open_one = meeting(is_adhoc=True, open_ended=True, offsets=())
    paused = replace(paused, paused_by_meeting_key=open_one.key)
    s = snap(now, occurrences=[open_one], timer=paused)
    assert kinds(reminders.compute(s, now), Kind.MEETING_END) == []


def test_overflow_is_keyed_by_the_set_of_items():
    now = at(17)
    plan = [line(item=1, allotted=120), line(item=2, task=12, allotted=120)]
    (overflow,) = kinds(reminders.compute(snap(now, plan=plan), now), Kind.OVERFLOW)
    assert overflow.due == now
    assert overflow.key == reminders.overflow_key(TODAY, [2, 1])
    assert overflow.key == reminders.overflow_key(TODAY, [1, 2])
    assert overflow.key != reminders.overflow_key(TODAY, [1])
    assert overflow.count == 2
    message = text(overflow, now)
    assert message.title == "2 tasks no longer fit today"
    assert [b.action for b in message.buttons] == [Action.MOVE_TOMORROW, Action.REVIEW_PLAN]
    assert message.view == View.PLAN
    evening = reminders.compute(snap(at(18, 30), plan=plan), at(18, 30))
    assert kinds(evening, Kind.OVERFLOW) == []
    saturday = reminders.compute(
        snap(at(10, day=SATURDAY), plan=plan, today=SATURDAY), at(10, day=SATURDAY)
    )
    assert kinds(saturday, Kind.OVERFLOW) == []


def test_open_adhoc_meeting_asks_at_end_of_day_and_after_a_restart():
    adhoc = meeting(start=at(14), is_adhoc=True, open_ended=True, offsets=(), title="Meeting")
    now = at(15)
    (still,) = kinds(reminders.compute(snap(now, occurrences=[adhoc]), now), Kind.OPEN_MEETING)
    assert still.key == "open_meeting:1:2026-11-03"
    assert still.due == at(17, 45)
    message = text(still, at(17, 45))
    assert message.title == "Still in “Meeting” (3h 45m)?"
    assert [b.action for b in message.buttons] == [Action.END_NOW, Action.STILL_IN]

    restarted = snap(now, occurrences=[adhoc], started_at=at(15), previous_check=at(14, 40))
    (still,) = kinds(reminders.compute(restarted, now), Kind.OPEN_MEETING)
    assert still.due == at(15)
    assert still.last_seen == at(14, 40)
    buttons = text(still, now).buttons
    assert [b.action for b in buttons] == [Action.END_NOW, Action.END_AT_LAST, Action.STILL_IN]
    assert buttons[1].label == "Ended at 14:40"


def test_start_and_end_of_day():
    now = at(8)
    plan = [line(carried=True), line(item=2, task=12, is_open=False)]
    result = reminders.compute(snap(now, plan=plan, follow_ups=2), now)
    (sod,) = kinds(result, Kind.SOD)
    (eod,) = kinds(result, Kind.EOD)
    assert (sod.key, sod.due) == ("sod:2026-11-03", at(9))
    assert (eod.key, eod.due) == ("eod:2026-11-03", at(17, 45))
    start = text(sod, at(9))
    assert start.title == "Plan your day"
    assert "1 task carried over." in start.body
    assert "2 follow-ups due." in start.body
    assert start.view == View.PLAN
    end = text(eod, at(17, 45))
    assert end.title == "Wrap up"
    assert end.body.startswith("1 of 2 planned tasks done.")
    assert end.view == View.REVIEW
    off = reminders.compute(snap(now, time_off={TODAY: TimeOffPart.FULL}), now)
    assert kinds(off, Kind.SOD) == kinds(off, Kind.EOD) == []
    # An afternoon off moves the end of the day to lunch.
    half = reminders.compute(snap(now, time_off={TODAY: TimeOffPart.PM}), now)
    assert kinds(half, Kind.EOD)[0].due == at(12, 45)


def test_autopause_at_a_meeting_start_while_a_timer_runs():
    standup = meeting()
    now = at(9, 30)
    (pause,) = kinds(
        reminders.compute(snap(now, occurrences=[standup], timer=running()), now), Kind.AUTOPAUSE
    )
    assert (pause.key, pause.due) == (f"autopause:{standup.key}", at(10))
    assert pause.tier == Tier.SILENT
    assert text(pause, now).title == ""
    off = Settings(auto_pause_meetings=False)
    result = reminders.compute(snap(now, occurrences=[standup], timer=running(), settings=off), now)
    assert kinds(result, Kind.AUTOPAUSE) == []
    assert kinds(reminders.compute(snap(now, occurrences=[standup]), now), Kind.AUTOPAUSE) == []


# --- Snooze, test, JSON ---------------------------------------------------------------------


def test_snooze_gives_a_new_key_two_minutes_later_and_snoozes_are_included():
    original = Reminder("meeting:1:x:10", Kind.MEETING, at(9, 50), name="Standup")
    first = reminders.snoozed(original, at(9, 51), 0)
    assert (first.key, first.due) == ("meeting:1:x:10:snooze1", at(9, 53))
    second = reminders.snoozed(first, at(9, 53), 1)
    assert second.key == "meeting:1:x:10:snooze2"
    now = at(9, 52)
    assert first in reminders.compute(snap(now, snoozes=(first,)), now)


def test_test_reminder_and_json_round_trip():
    test = reminders.test_reminder(at(9))
    assert test.key == "test:2026-11-03T08:00:00Z"
    message = text(test, at(9))
    assert [b.action for b in message.buttons] == [Action.WORKS, Action.SNOOZE]
    full = Reminder(
        "k",
        Kind.OPEN_MEETING,
        at(9),
        day=TODAY,
        meeting_id=3,
        original_start=at(8),
        start=at(8),
        end=at(9),
        last_seen=at(8, 30),
        lines=("a", "b"),
    )
    assert Reminder.from_json(full.to_json()) == full
    assert Reminder.from_json(test.to_json()) == test


# --- Late reminders (missed summary) --------------------------------------------------------


def test_on_time_reminders_fire_and_late_ones_are_summarised_once():
    standup = meeting()
    now = at(11)  # back from a long suspend
    due = [
        r
        for r in reminders.compute(snap(at(9), occurrences=[standup]), at(9))
        if r.kind == Kind.MEETING
    ]
    up = Reminder("allot_up:x", Kind.ALLOT_UP, at(10, 30), name="PROJ-123")
    drift = Reminder("drift:x", Kind.DRIFT, at(10), task_id=1)
    fresh = Reminder("overflow:x", Kind.OVERFLOW, now, day=TODAY)
    result = reminders.triage([*due, up, drift, fresh], now)
    assert result.fire == (fresh,)
    assert set(result.missed) == {*due, up, drift}
    # The ended standup is listed once, however many offsets it had.
    assert [r.kind for r in result.summary] == [Kind.MEETING, Kind.ALLOT_UP]
    summary = reminders.summary_reminder(result.summary, now, BERLIN, True)
    assert summary.lines == ("Standup 10:00 (missed)", "PROJ-123 allotment ended")
    message = text(summary, now)
    assert message.title == "While you were away"
    assert message.body == "Standup 10:00 (missed)\nPROJ-123 allotment ended"


def test_a_late_meeting_that_has_not_ended_fires_once_live():
    standup = meeting(start=at(10), minutes=30)
    due = [
        r
        for r in reminders.compute(snap(at(9), occurrences=[standup]), at(9))
        if r.kind == Kind.MEETING
    ]
    now = at(10, 4)
    result = reminders.triage(due, now)
    (live,) = result.fire
    assert live.minutes == 1  # the latest offset
    assert result.missed == (due[0],)
    assert result.summary == ()
    assert text(live, now).title == "Standup started 4 min ago"


def test_late_reminders_that_still_matter_fire():
    now = at(12)
    ongoing = at(13)
    back = Reminder("meeting_end:x", Kind.MEETING_END, at(11))
    still = Reminder("open_meeting:x", Kind.OPEN_MEETING, at(11))
    pause = Reminder("autopause:x", Kind.AUTOPAUSE, at(11), end=ongoing)
    stale_pause = Reminder("autopause:y", Kind.AUTOPAUSE, at(10), end=at(11))
    sod = Reminder("sod:x", Kind.SOD, at(9), day=TODAY)
    warn = Reminder("allot_warn:x", Kind.ALLOT_WARN, at(11))
    result = reminders.triage([back, still, pause, stale_pause, sod, warn], now)
    assert set(result.fire) == {back, still, pause}
    assert set(result.missed) == {stale_pause, sod, warn}
    assert result.summary == (sod,)
    assert reminders.summary_line(sod, BERLIN, True) == "Start of day"
    assert reminders.summary_line(replace(sod, kind=Kind.EOD), BERLIN, True) == "Wrap up"
    assert reminders.summary_line(replace(sod, kind=Kind.DRIFT, name="X"), BERLIN, True) == "X"


def test_within_the_grace_period_reminders_fire_normally():
    reminder = Reminder("sod:x", Kind.SOD, at(9), day=TODAY)
    assert reminders.triage([reminder], at(9, 2)).fire == (reminder,)
    assert reminders.triage([reminder], at(9, 3)).fire == ()


# --- Moot reminders -------------------------------------------------------------------------


def test_reminders_become_moot():
    standup = meeting()
    now = at(9, 55)
    s = snap(now, occurrences=[standup], plan=[line()])
    (ten, _one) = kinds(reminders.compute(s, at(9)), Kind.MEETING)
    assert reminders.still_relevant(ten, s, now)
    assert not reminders.still_relevant(ten, s, at(10, 20))  # over
    assert not reminders.still_relevant(ten, snap(now, plan=[line()]), now)  # cancelled

    up = Reminder("u", Kind.ALLOT_UP, now, item_id=1, minutes=60)
    assert reminders.still_relevant(up, s, now)
    assert not reminders.still_relevant(replace(up, minutes=45), s, now)  # extended since
    assert not reminders.still_relevant(up, snap(now, plan=[line(is_open=False)]), now)  # done

    drift = Reminder("d", Kind.DRIFT, now, item_id=1)
    assert reminders.still_relevant(drift, s, now)
    timed = snap(now, plan=[line()], timer=running())
    assert not reminders.still_relevant(drift, timed, now)

    back = Reminder("b", Kind.MEETING_END, now, meeting_id=1, original_start=standup.start)
    paused = TimerState(11, TimerRun.PAUSED, None, now, PauseReason.MEETING, standup.key)
    assert reminders.still_relevant(back, snap(now, timer=paused), now)
    assert not reminders.still_relevant(back, timed, now)

    plan = [line(item=1, allotted=600)]
    late = snap(at(17), plan=plan)
    (overflow,) = kinds(reminders.compute(late, at(17)), Kind.OVERFLOW)
    assert reminders.still_relevant(overflow, late, at(17))
    assert not reminders.still_relevant(overflow, snap(at(17), plan=[line(allotted=30)]), at(17))

    sod = Reminder("s", Kind.SOD, now, day=TODAY)
    assert reminders.still_relevant(sod, s, now)
    assert not reminders.still_relevant(replace(sod, day=TODAY - timedelta(days=1)), s, now)
    assert reminders.still_relevant(reminders.test_reminder(now), s, now)

    adhoc = meeting(mid=2, is_adhoc=True, open_ended=True, offsets=())
    still = Reminder("o", Kind.OPEN_MEETING, now, meeting_id=2, original_start=adhoc.start)
    assert reminders.still_relevant(still, snap(now, occurrences=[adhoc]), at(10, 30))
    ended = replace(adhoc, open_ended=False, duration_min=60)
    assert not reminders.still_relevant(still, snap(now, occurrences=[ended]), at(10, 30))
