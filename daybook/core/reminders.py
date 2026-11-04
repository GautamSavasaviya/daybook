"""Reminder computation (PLAN §5.5). Pure: a snapshot of the day in, reminders out.

`compute` lists every reminder the snapshot implies, each with a unique key and a due
time. The scheduler fires those that became due since its last check and logs each key
once (at most once, D-024). Allotment reminders depend on *tracked* time, so they are
recomputed on every change rather than stored. `triage` decides what happens to reminders
noticed late (D-027), `still_relevant` when a shown one has become moot (D-054), and
`render` the words and buttons (D-053, at most three buttons, titles ≤ 40 characters).
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, tzinfo
from enum import StrEnum
from gettext import gettext as _
from gettext import ngettext
from typing import Any

from daybook.core import timeutil
from daybook.core.calendar import Interval, WorkCalendar
from daybook.core.meetings import effective_end
from daybook.core.models import (
    Occurrence,
    PauseReason,
    TimeOffPart,
    TimerState,
    occurrence_key,
)
from daybook.core.planner import Schedule
from daybook.core.settings import Settings

GRACE = timedelta(minutes=2)  # later than this counts as missed (D-027)
SNOOZE = timedelta(minutes=2)
EXTEND_MIN = 15
EOD_LEAD = timedelta(minutes=15)
TITLE_MAX = 40


class Tier(StrEnum):
    STAY = "stay"  # URGENT: the banner stays until acted on
    ACT = "act"  # HIGH
    INFO = "info"  # NORMAL
    SILENT = "silent"  # no notification: an action the app takes itself


class Kind(StrEnum):
    MEETING = "meeting"
    ALLOT_WARN = "allot_warn"
    ALLOT_UP = "allot_up"
    DRIFT = "drift"
    MEETING_END = "meeting_end"
    OVERFLOW = "overflow"
    OPEN_MEETING = "open_meeting"
    SOD = "sod"
    EOD = "eod"
    SUMMARY = "summary"
    TEST = "test"
    AUTOPAUSE = "autopause"


# Every kind declares its tier (D-053); tests fail for a kind missing here.
TIERS: dict[Kind, Tier] = {
    Kind.MEETING: Tier.STAY,
    Kind.ALLOT_UP: Tier.STAY,
    Kind.OPEN_MEETING: Tier.STAY,
    Kind.TEST: Tier.STAY,
    Kind.DRIFT: Tier.ACT,
    Kind.MEETING_END: Tier.ACT,
    Kind.OVERFLOW: Tier.ACT,
    Kind.ALLOT_WARN: Tier.INFO,
    Kind.SOD: Tier.INFO,
    Kind.EOD: Tier.INFO,
    Kind.SUMMARY: Tier.INFO,
    Kind.AUTOPAUSE: Tier.SILENT,
}

# GSound event ids per tier; the 10-minute warning is silent (D-053).
SOUNDS: dict[Tier, str] = {
    Tier.STAY: "alarm-clock-elapsed",
    Tier.ACT: "message-new-instant",
    Tier.INFO: "message-new-instant",
}
SILENT_KINDS = frozenset({Kind.ALLOT_WARN, Kind.AUTOPAUSE})


class Action(StrEnum):
    OPEN = "open"  # the notification itself was clicked
    JOIN = "join"
    SNOOZE = "snooze"
    SWITCH = "switch"
    EXTEND = "extend"
    DONE = "done"
    START = "start"
    SKIP = "skip"
    RESUME = "resume"
    PICK = "pick"
    MOVE_TOMORROW = "move"
    REVIEW_PLAN = "review"
    END_NOW = "end"
    END_AT_LAST = "end_last"
    STILL_IN = "still"
    WORKS = "works"
    DISMISSED = "dismissed"


class View(StrEnum):
    TODAY = "today"
    PLAN = "plan"
    REVIEW = "review"


@dataclass(frozen=True)
class Reminder:
    """One reminder occurrence. Everything needed to show it and act on it is in here, so
    it can be stored as JSON and acted on after a restart (D-029)."""

    key: str
    kind: Kind
    due: datetime
    day: date | None = None
    task_id: int | None = None
    item_id: int | None = None
    meeting_id: int | None = None
    original_start: datetime | None = None
    start: datetime | None = None  # the meeting's start
    end: datetime | None = None  # the meeting's (expected) end
    name: str = ""  # the meeting's title or the task's ticket key / title
    link: str | None = None
    minutes: int | None = None  # offset, allotment, overflowing minutes
    count: int = 0  # overflowing tasks, carried items, done items
    total: int = 0  # follow-ups due, planned items, the warning's minutes
    next_task_id: int | None = None
    next_name: str = ""
    last_seen: datetime | None = None  # open meeting after a restart: "ended at …"
    meeting_title: str = ""  # back-to-task: the meeting that ended
    lines: tuple[str, ...] = field(default=())  # the missed summary

    @property
    def tier(self) -> Tier:
        return TIERS[self.kind]

    @property
    def occurrence(self) -> str | None:
        if self.meeting_id is None or self.original_start is None:
            return None
        return occurrence_key(self.meeting_id, self.original_start)

    @property
    def subject(self) -> str:
        """The notification id: a newer reminder for the same subject replaces the older
        banner (D-054)."""
        match self.kind:
            case Kind.MEETING | Kind.OPEN_MEETING | Kind.AUTOPAUSE:
                return f"meeting:{self.occurrence}"
            case Kind.ALLOT_WARN | Kind.ALLOT_UP | Kind.DRIFT | Kind.MEETING_END:
                return f"task:{self.task_id}"
            case Kind.OVERFLOW | Kind.SOD | Kind.EOD:
                return f"day:{self.day}"
            case Kind.SUMMARY:
                return "summary"
            case Kind.TEST:
                return "test"

    def to_json(self) -> str:
        data: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, datetime):
                value = timeutil.to_db(value)
            elif isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, tuple):
                value = list(value)
            data[f.name] = value
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Reminder:
        data = json.loads(text)
        for name in ("due", "original_start", "start", "end", "last_seen"):
            if data.get(name):
                data[name] = timeutil.from_db(data[name])
        if data.get("day"):
            data["day"] = date.fromisoformat(data["day"])
        data["kind"] = Kind(data["kind"])
        data["lines"] = tuple(data.get("lines", ()))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class PlanLine:
    """One of today's plan items, as the reminders need it."""

    item_id: int
    task_id: int
    name: str
    allotted_min: int | None  # None = Rest of day
    tracked_s: int
    open: bool
    carried: bool = False


@dataclass(frozen=True)
class Snapshot:
    today: date
    tz: tzinfo
    settings: Settings
    calendar: WorkCalendar
    occurrences: tuple[Occurrence, ...]  # today's and tomorrow's
    plan: tuple[PlanLine, ...]  # today's
    schedule: Schedule  # today's, from now
    timer: TimerState | None
    timer_task_name: str = ""
    idle_since: datetime | None = None  # when timer time last ended today
    snoozes: tuple[Reminder, ...] = ()
    started_at: datetime | None = None  # this app run's start (open meetings on restart)
    previous_check: datetime | None = None  # the last check before this run
    follow_ups: int = 0


# Computing ----------------------------------------------------------------------------------


def _day_key(day: date) -> str:
    return day.isoformat()


def _work_allows(snapshot: Snapshot, instant: datetime) -> bool:
    """Day reminders (start/end of day, drift, allotment) need a working day, and none
    are due inside a half day off (D-048)."""
    day = instant.astimezone(snapshot.tz).date()
    if not snapshot.calendar.is_working(day):
        return False
    leave = snapshot.calendar.leave(day, snapshot.tz)
    return leave is None or not leave.contains(instant)


def _meeting_allows(snapshot: Snapshot, occurrence: Occurrence) -> bool:
    if snapshot.settings.meeting_reminders_on_leave:
        return True
    day = occurrence.start.astimezone(snapshot.tz).date()
    if snapshot.calendar.time_off(day) == TimeOffPart.FULL:
        return False
    leave = snapshot.calendar.leave(day, snapshot.tz)
    return leave is None or not leave.contains(occurrence.start)


def _meeting(
    occurrence: Occurrence, kind: Kind, key: str, due: datetime, now: datetime
) -> Reminder:
    return Reminder(
        key=key,
        kind=kind,
        due=due,
        meeting_id=occurrence.meeting_id,
        original_start=occurrence.original_start,
        start=occurrence.start,
        end=effective_end(occurrence, now),
        name=occurrence.title,
        link=occurrence.link,
    )


def _meetings(snapshot: Snapshot, now: datetime) -> list[Reminder]:
    result: list[Reminder] = []
    pausing = (
        snapshot.settings.auto_pause_meetings
        and snapshot.timer is not None
        and snapshot.timer.running
    )
    for occurrence in snapshot.occurrences:
        if occurrence.is_adhoc:
            continue  # "Meeting now" pauses the timer itself and has no offsets
        if _meeting_allows(snapshot, occurrence):
            for offset in occurrence.reminder_offsets:
                key = f"meeting:{occurrence.key}:{offset}"
                due = occurrence.start - timedelta(minutes=offset)
                result.append(
                    replace(_meeting(occurrence, Kind.MEETING, key, due, now), minutes=offset)
                )
        if pausing:
            key = f"autopause:{occurrence.key}"
            result.append(_meeting(occurrence, Kind.AUTOPAUSE, key, occurrence.start, now))
    return result


def _timed_line(snapshot: Snapshot) -> PlanLine | None:
    timer = snapshot.timer
    if timer is None or not timer.running:
        return None
    return next(
        (
            line
            for line in snapshot.plan
            if line.task_id == timer.task_id and line.open and line.allotted_min is not None
        ),
        None,
    )


def _next_after(snapshot: Snapshot, item_id: int, now: datetime) -> PlanLine | None:
    segment = snapshot.schedule.next_task_after(now, item_id)
    if segment is None:
        return None
    return next((line for line in snapshot.plan if line.item_id == segment.plan_item_id), None)


def _allotment(snapshot: Snapshot, now: datetime) -> list[Reminder]:
    """Only while the timer runs on a timed item; never for Rest of day (D-050)."""
    line = _timed_line(snapshot)
    if line is None or line.allotted_min is None:
        return []
    remaining = timedelta(seconds=line.allotted_min * 60 - line.tracked_s)
    following = _next_after(snapshot, line.item_id, now)
    base = Reminder(
        key="",
        kind=Kind.ALLOT_UP,
        due=now + remaining,
        day=snapshot.today,
        task_id=line.task_id,
        item_id=line.item_id,
        name=line.name,
        minutes=line.allotted_min,
        next_task_id=following.task_id if following else None,
        next_name=following.name if following else "",
    )
    suffix = f"{_day_key(snapshot.today)}:{line.item_id}:{line.allotted_min}"
    result = [replace(base, key=f"allot_up:{suffix}")]
    warn = snapshot.settings.allotment_warning_min
    if warn > 0 and line.allotted_min > warn:
        due = now + remaining - timedelta(minutes=warn)
        result.append(
            replace(base, key=f"allot_warn:{suffix}", kind=Kind.ALLOT_WARN, due=due, total=warn)
        )
    return [r for r in result if _work_allows(snapshot, r.due)]


def _busy(snapshot: Snapshot, now: datetime, window: Interval) -> list[Interval]:
    blocks = [
        Interval(o.start, effective_end(o, now))
        for o in snapshot.occurrences
        if o.start < window.end and effective_end(o, now) > window.start
    ]
    lunch = snapshot.calendar.lunch(snapshot.today, snapshot.tz)
    if lunch is not None:
        blocks.append(lunch)
    blocks.sort(key=lambda b: b.start)
    merged: list[Interval] = []
    for block in blocks:
        if merged and block.start <= merged[-1].end:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, block.end))
        else:
            merged.append(block)
    return merged


def _drift(snapshot: Snapshot, now: datetime) -> list[Reminder]:
    """ "Planned now: … — start it?" once per planned item and free window, after
    `drift_after_min` with no timer running (D-025)."""
    timer = snapshot.timer
    if timer is not None and timer.running:
        return []
    window = snapshot.calendar.day_window(snapshot.today, snapshot.tz)
    if window is None or not window.contains(now):
        return []
    busy = _busy(snapshot, now, window)
    if any(block.contains(now) for block in busy):
        return []
    before = [block for block in busy if block.end <= now]
    free_start = max([window.start, *(block.end for block in before)])
    free_end = min([window.end, *(block.start for block in busy if block.start > now)])
    anchor = max(free_start, snapshot.idle_since or free_start)
    due = anchor + timedelta(minutes=snapshot.settings.drift_after_min)
    if due >= free_end:
        return []
    # Already idle for longer (the app just started, or came back from suspend): the
    # nudge is still true, so it is due now rather than dropped as missed.
    due = max(due, now)
    segment = next((s for s in snapshot.schedule.task_segments() if s.start < free_end), None)
    line = next(
        (p for p in snapshot.plan if segment is not None and p.item_id == segment.plan_item_id),
        None,
    )
    if line is None or not line.open or not _work_allows(snapshot, due):
        return []
    key = f"drift:{_day_key(snapshot.today)}:{line.item_id}:{len(before)}"
    return [
        Reminder(
            key=key,
            kind=Kind.DRIFT,
            due=due,
            day=snapshot.today,
            task_id=line.task_id,
            item_id=line.item_id,
            name=line.name,
        )
    ]


def _meeting_end(snapshot: Snapshot) -> list[Reminder]:
    """ "Back to PROJ-123?" when the meeting that paused the timer ends."""
    timer = snapshot.timer
    if timer is None or timer.running or timer.pause_reason != PauseReason.MEETING:
        return []
    occurrence = next(
        (o for o in snapshot.occurrences if o.key == timer.paused_by_meeting_key), None
    )
    if occurrence is None or occurrence.open_ended:
        return []
    return [
        Reminder(
            key=f"meeting_end:{occurrence.key}",
            kind=Kind.MEETING_END,
            due=occurrence.end,
            task_id=timer.task_id,
            meeting_id=occurrence.meeting_id,
            original_start=occurrence.original_start,
            start=occurrence.start,
            end=occurrence.end,
            name=snapshot.timer_task_name,
            meeting_title=occurrence.title,
        )
    ]


def overflow_key(day: date, item_ids: Sequence[int]) -> str:
    digest = hashlib.sha1(",".join(str(i) for i in sorted(item_ids)).encode()).hexdigest()
    return f"overflow:{_day_key(day)}:{digest[:8]}"


def _overflow(snapshot: Snapshot, now: datetime) -> list[Reminder]:
    """A new set of items that no longer fit today is reported once, right away (D-056)."""
    overflow = snapshot.schedule.overflow
    if not overflow or not snapshot.schedule.working or not _work_allows(snapshot, now):
        return []
    window = snapshot.calendar.day_window(snapshot.today, snapshot.tz)
    if window is None or now >= window.end:
        return []  # after work, unfinished items simply carry over (D-104)
    return [
        Reminder(
            key=overflow_key(snapshot.today, [o.plan_item_id for o in overflow]),
            kind=Kind.OVERFLOW,
            due=now,
            day=snapshot.today,
            minutes=sum(o.minutes or 0 for o in overflow),
            count=len(overflow),
        )
    ]


def _open_meetings(snapshot: Snapshot, now: datetime) -> list[Reminder]:
    """ "Still in 'Meeting'?" for an ad-hoc meeting left open: at the end-of-day time, or
    right after a restart (PLAN §5.3)."""
    window = snapshot.calendar.day_window(snapshot.today, snapshot.tz)
    result: list[Reminder] = []
    for occurrence in snapshot.occurrences:
        if not (occurrence.is_adhoc and occurrence.open_ended) or occurrence.start > now:
            continue
        restarted = snapshot.started_at is not None and occurrence.start < snapshot.started_at
        if restarted and snapshot.started_at is not None:
            due = snapshot.started_at
        elif window is not None and window.end - EOD_LEAD > occurrence.start:
            due = window.end - EOD_LEAD
        else:
            continue
        key = f"open_meeting:{occurrence.meeting_id}:{_day_key(snapshot.today)}"
        reminder = _meeting(occurrence, Kind.OPEN_MEETING, key, due, now)
        if restarted and snapshot.previous_check is not None:
            reminder = replace(reminder, last_seen=max(snapshot.previous_check, occurrence.start))
        result.append(replace(reminder, day=snapshot.today))
    return result


def _day(snapshot: Snapshot) -> list[Reminder]:
    window = snapshot.calendar.day_window(snapshot.today, snapshot.tz)
    if window is None:
        return []
    today = _day_key(snapshot.today)
    carried = sum(1 for line in snapshot.plan if line.carried and line.open)
    done = sum(1 for line in snapshot.plan if not line.open)
    start = Reminder(
        key=f"sod:{today}",
        kind=Kind.SOD,
        due=window.start,
        day=snapshot.today,
        count=carried,
        total=snapshot.follow_ups,
    )
    end = Reminder(
        key=f"eod:{today}",
        kind=Kind.EOD,
        due=window.end - EOD_LEAD,
        day=snapshot.today,
        count=done,
        total=len(snapshot.plan),
    )
    return [r for r in (start, end) if _work_allows(snapshot, r.due)]


def compute(snapshot: Snapshot, now: datetime) -> list[Reminder]:
    """Every reminder the snapshot implies, past and future, sorted by due time."""
    result = [
        *_meetings(snapshot, now),
        *_allotment(snapshot, now),
        *_drift(snapshot, now),
        *_meeting_end(snapshot),
        *_overflow(snapshot, now),
        *_open_meetings(snapshot, now),
        *_day(snapshot),
        *snapshot.snoozes,
    ]
    result.sort(key=lambda r: (r.due, r.key))
    return result


def snoozed(reminder: Reminder, now: datetime, existing: int) -> Reminder:
    """The same reminder again in 2 minutes, with its own key (`…:snooze{n}`)."""
    base = reminder.key.split(":snooze")[0]
    return replace(reminder, key=f"{base}:snooze{existing + 1}", due=now + SNOOZE)


def test_reminder(now: datetime) -> Reminder:
    return Reminder(key=f"test:{timeutil.to_db(now)}", kind=Kind.TEST, due=now)


# Late reminders -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Triage:
    fire: tuple[Reminder, ...]
    missed: tuple[Reminder, ...]  # logged as missed, not shown one by one
    summary: tuple[Reminder, ...]  # the missed ones worth listing in the summary


def triage(due: Sequence[Reminder], now: datetime) -> Triage:
    """Split reminders that became due since the last check (D-027).

    On time (within 2 min): fire. Late: a meeting that hasn't ended fires once (its latest
    offset) and its other offsets go quietly; reminders still worth acting on fire; the
    rest are logged as missed and listed in one summary.
    """
    fire: list[Reminder] = []
    missed: list[Reminder] = []
    summary: list[Reminder] = []
    live_meetings: dict[str, Reminder] = {}
    for reminder in sorted(due, key=lambda r: (r.due, r.key)):
        if now - reminder.due <= GRACE:
            fire.append(reminder)
            continue
        ongoing = reminder.end is not None and reminder.end > now
        match reminder.kind:
            case Kind.MEETING if ongoing:
                occurrence = reminder.occurrence or reminder.key
                earlier = live_meetings.get(occurrence)
                if earlier is not None:
                    missed.append(earlier)
                live_meetings[occurrence] = reminder
            case Kind.MEETING:
                missed.append(reminder)
                if all(s.occurrence != reminder.occurrence for s in summary):
                    summary.append(reminder)
            case Kind.AUTOPAUSE if ongoing:
                fire.append(reminder)
            case Kind.MEETING_END | Kind.OPEN_MEETING | Kind.OVERFLOW | Kind.TEST:
                fire.append(reminder)
            case Kind.ALLOT_UP | Kind.SOD | Kind.EOD:
                missed.append(reminder)
                summary.append(reminder)
            case _:
                missed.append(reminder)
    fire.extend(live_meetings.values())
    fire.sort(key=lambda r: (r.due, r.key))
    return Triage(tuple(fire), tuple(missed), tuple(summary))


def summary_reminder(
    missed: Sequence[Reminder], now: datetime, tz: tzinfo, use_24h: bool
) -> Reminder:
    lines = tuple(summary_line(r, tz, use_24h) for r in missed)
    return Reminder(key=f"summary:{timeutil.to_db(now)}", kind=Kind.SUMMARY, due=now, lines=lines)


def summary_line(reminder: Reminder, tz: tzinfo, use_24h: bool) -> str:
    def at(instant: datetime | None) -> str:
        return timeutil.fmt_clock(instant, tz, use_24h=use_24h) if instant else ""

    match reminder.kind:
        case Kind.MEETING:
            return _("{title} {time} (missed)").format(title=reminder.name, time=at(reminder.start))
        case Kind.ALLOT_UP:
            return _("{task} allotment ended").format(task=reminder.name)
        case Kind.SOD:
            return _("Start of day")
        case Kind.EOD:
            return _("Wrap up")
        case _:
            return reminder.name


# Moot reminders -----------------------------------------------------------------------------


def _occurrence_live(reminder: Reminder, snapshot: Snapshot, now: datetime) -> bool:
    occurrence = next((o for o in snapshot.occurrences if o.key == reminder.occurrence), None)
    if occurrence is None or effective_end(occurrence, now) <= now:
        return False
    return reminder.kind == Kind.MEETING or occurrence.open_ended


def _line_live(reminder: Reminder, snapshot: Snapshot) -> PlanLine | None:
    line = next((p for p in snapshot.plan if p.item_id == reminder.item_id), None)
    return line if line is not None and line.open else None


def _timer_live(reminder: Reminder, snapshot: Snapshot, _now: datetime) -> bool:
    timer = snapshot.timer
    running = timer is not None and timer.running
    if reminder.kind == Kind.DRIFT:
        return _line_live(reminder, snapshot) is not None and not running
    # Back to the task: only while the timer is still paused by that meeting.
    return timer is not None and not running and timer.paused_by_meeting_key == reminder.occurrence


def _allotment_live(reminder: Reminder, snapshot: Snapshot, _now: datetime) -> bool:
    line = _line_live(reminder, snapshot)
    return line is not None and line.allotted_min == reminder.minutes


def _overflow_live(reminder: Reminder, snapshot: Snapshot, _now: datetime) -> bool:
    ids = [o.plan_item_id for o in snapshot.schedule.overflow]
    return bool(ids) and overflow_key(snapshot.today, ids) == reminder.key


def _day_live(reminder: Reminder, snapshot: Snapshot, _now: datetime) -> bool:
    return reminder.day == snapshot.today


def _always(_reminder: Reminder, _snapshot: Snapshot, _now: datetime) -> bool:
    return True  # the summary and the test reminder stay until handled


_RELEVANCE: dict[Kind, Callable[[Reminder, Snapshot, datetime], bool]] = {
    Kind.MEETING: _occurrence_live,
    Kind.OPEN_MEETING: _occurrence_live,
    Kind.ALLOT_WARN: _allotment_live,
    Kind.ALLOT_UP: _allotment_live,
    Kind.DRIFT: _timer_live,
    Kind.MEETING_END: _timer_live,
    Kind.OVERFLOW: _overflow_live,
    Kind.SOD: _day_live,
    Kind.EOD: _day_live,
    Kind.SUMMARY: _always,
    Kind.TEST: _always,
    Kind.AUTOPAUSE: _always,
}


def still_relevant(reminder: Reminder, snapshot: Snapshot, now: datetime) -> bool:
    """False once a shown reminder no longer applies, so it is withdrawn (D-054): the
    meeting is over or gone, the task is done, held, moved or extended, a timer started,
    the plan was fixed, or the day is over."""
    return _RELEVANCE[reminder.kind](reminder, snapshot, now)


# Words and buttons --------------------------------------------------------------------------


@dataclass(frozen=True)
class Button:
    label: str
    action: Action


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    buttons: tuple[Button, ...]
    view: View  # where clicking the notification goes


@dataclass(frozen=True)
class _Context:
    now: datetime
    tz: tzinfo
    use_24h: bool

    def at(self, instant: datetime | None) -> str:
        return timeutil.fmt_clock(instant, self.tz, use_24h=self.use_24h) if instant else ""


def _fit(template: str, name: str, **values: str) -> str:
    """Fill `template`, shortening `name` so the title stays within 40 characters."""
    room = TITLE_MAX - len(template.format(name="", **values))
    if len(name) > room:
        name = name[: max(1, room - 1)].rstrip() + "…"
    return template.format(name=name, **values)


def _render_meeting(r: Reminder, c: _Context) -> Message:
    start = r.start or r.due
    if start > c.now + timedelta(seconds=30):
        minutes = max(1, round((start - c.now).total_seconds() / 60))
        title = _fit(_("{name} in {n} min"), r.name, n=str(minutes))
    elif start >= c.now - timedelta(minutes=1):
        title = _fit(_("{name} starts now"), r.name)
    else:
        ago = round((c.now - start).total_seconds() / 60)
        title = _fit(_("{name} started {n} min ago"), r.name, n=str(ago))
    body = _("{start} to {end}").format(start=c.at(start), end=c.at(r.end))
    buttons = (Button(_("Join"), Action.JOIN),) if r.link else ()
    return Message(title, body, (*buttons, Button(_("Snooze 2 min"), Action.SNOOZE)), View.TODAY)


def _render_allot_warn(r: Reminder, c: _Context) -> Message:
    left = max(0, round((r.due - c.now).total_seconds() / 60) + r.total)
    title = _fit(_("{name}: {n} min left"), r.name, n=str(left))
    body = _("Planned {planned} today.").format(planned=timeutil.fmt_duration(r.minutes or 0))
    return Message(title, body, (Button(_("+15 min"), Action.EXTEND),), View.TODAY)


def _render_allot_up(r: Reminder, c: _Context) -> Message:
    planned = r.minutes or 0
    spent = planned * 60 + max(0, int((c.now - r.due).total_seconds()))
    body = _("Planned {planned}, spent {spent}.").format(
        planned=timeutil.fmt_duration(planned), spent=timeutil.fmt_duration(spent // 60)
    )
    buttons: tuple[Button, ...] = (
        Button(_("+15 min"), Action.EXTEND),
        Button(_("Mark Done"), Action.DONE),
    )
    if r.next_task_id is not None:
        body += " " + _("Next: {task}").format(task=r.next_name)
        buttons = (Button(_("Switch to Next"), Action.SWITCH), *buttons)
    return Message(_fit(_("Time's up: {name}"), r.name), body, buttons, View.TODAY)


def _render_drift(r: Reminder, _c: _Context) -> Message:
    buttons = (Button(_("Start"), Action.START), Button(_("Skip"), Action.SKIP))
    return Message(
        _fit(_("Planned now: {name}"), r.name),
        _("No timer is running. Start it?"),
        buttons,
        View.TODAY,
    )


def _render_meeting_end(r: Reminder, _c: _Context) -> Message:
    buttons = (Button(_("Resume"), Action.RESUME), Button(_("Pick Another"), Action.PICK))
    body = _("“{meeting}” has ended.").format(meeting=r.meeting_title)
    return Message(_fit(_("Back to {name}?"), r.name), body, buttons, View.TODAY)


def _render_overflow(r: Reminder, _c: _Context) -> Message:
    title = ngettext(
        "{n} task no longer fits today", "{n} tasks no longer fit today", r.count
    ).format(n=r.count)
    body = _("{time} doesn't fit before the end of the day.").format(
        time=timeutil.fmt_duration(r.minutes or 0)
    )
    buttons = (
        Button(_("Move to Tomorrow"), Action.MOVE_TOMORROW),
        Button(_("Review Plan"), Action.REVIEW_PLAN),
    )
    return Message(title, body, buttons, View.PLAN)


def _render_open_meeting(r: Reminder, c: _Context) -> Message:
    length = timeutil.fmt_duration(max(0, int((c.now - (r.start or c.now)).total_seconds() // 60)))
    title = _fit(_("Still in “{name}” ({time})?"), r.name, time=length)
    body = _("It started at {time} and has no end yet.").format(time=c.at(r.start))
    buttons = [Button(_("End Now"), Action.END_NOW)]
    if r.last_seen is not None:
        label = _("Ended at {time}").format(time=c.at(r.last_seen))
        buttons.append(Button(label, Action.END_AT_LAST))
    buttons.append(Button(_("Still in It"), Action.STILL_IN))
    return Message(title, body, tuple(buttons), View.TODAY)


def _render_sod(r: Reminder, _c: _Context) -> Message:
    parts = [_("Pick today's tasks and give each a time.")]
    if r.count:
        parts.append(
            ngettext("{n} task carried over.", "{n} tasks carried over.", r.count).format(n=r.count)
        )
    if r.total:
        parts.append(
            ngettext("{n} follow-up due.", "{n} follow-ups due.", r.total).format(n=r.total)
        )
    return Message(
        _("Plan your day"), " ".join(parts), (Button(_("Plan"), Action.OPEN),), View.PLAN
    )


def _render_eod(r: Reminder, _c: _Context) -> Message:
    body = _("{done} of {total} planned tasks done. Unfinished work carries over.").format(
        done=r.count, total=r.total
    )
    return Message(_("Wrap up"), body, (Button(_("Review Day"), Action.OPEN),), View.REVIEW)


def _render_summary(r: Reminder, _c: _Context) -> Message:
    return Message(_("While you were away"), "\n".join(r.lines), (), View.TODAY)


def _render_test(_r: Reminder, _c: _Context) -> Message:
    body = _("If you see this, reminders work. It stays until you act on it.")
    buttons = (Button(_("Works"), Action.WORKS), Button(_("Snooze 2 min"), Action.SNOOZE))
    return Message(_("Test reminder"), body, buttons, View.TODAY)


def _render_silent(_r: Reminder, _c: _Context) -> Message:
    return Message("", "", (), View.TODAY)


_RENDERERS: dict[Kind, Callable[[Reminder, _Context], Message]] = {
    Kind.MEETING: _render_meeting,
    Kind.ALLOT_WARN: _render_allot_warn,
    Kind.ALLOT_UP: _render_allot_up,
    Kind.DRIFT: _render_drift,
    Kind.MEETING_END: _render_meeting_end,
    Kind.OVERFLOW: _render_overflow,
    Kind.OPEN_MEETING: _render_open_meeting,
    Kind.SOD: _render_sod,
    Kind.EOD: _render_eod,
    Kind.SUMMARY: _render_summary,
    Kind.TEST: _render_test,
    Kind.AUTOPAUSE: _render_silent,
}


def render(reminder: Reminder, now: datetime, tz: tzinfo, use_24h: bool) -> Message:
    """The title (subject first, at most 40 characters), body, buttons (at most three) and
    the view a click opens. Rendered when shown, so "in 3 min" is true at that moment."""
    return _RENDERERS[reminder.kind](reminder, _Context(now, tz, use_24h))
