"""Auto-schedule: fit the day's plan around meetings and lunch (PLAN §5.1).

Pure and deterministic: the same inputs always give the same schedule, and nothing here
reads the clock or the database.

1. The usable window is the working window from `now` on.
2. Meetings and lunch are busy blocks; overlapping or touching blocks merge.
3. The gaps are free windows, numbered from 0. Gaps under 5 minutes are not used (D-015).
4. Timed items need `allotted - tracked` minutes. Rest-of-day items share what is left
   equally, the earliest getting the spare minutes (D-050).
5. Items are placed in plan order, splitting around busy blocks. What doesn't fit before
   the window ends is overflow. Rest-of-day items never overflow.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from daybook.core.calendar import Interval

MIN_FRAGMENT_MIN = 5


class SegmentKind(StrEnum):
    TASK = "task"
    MEETING = "meeting"
    LUNCH = "lunch"


@dataclass(frozen=True)
class Busy:
    """A meeting occurrence as the planner sees it (UTC)."""

    start: datetime
    end: datetime
    key: str
    title: str = ""


@dataclass(frozen=True)
class PlanInput:
    plan_item_id: int
    task_id: int
    allotted_min: int | None  # None = Rest of day
    tracked_min: int = 0

    @property
    def remaining_min(self) -> int | None:
        if self.allotted_min is None:
            return None
        return max(0, self.allotted_min - self.tracked_min)


@dataclass(frozen=True)
class Segment:
    kind: SegmentKind
    start: datetime
    end: datetime
    plan_item_id: int | None = None
    task_id: int | None = None
    part: int = 0  # 1, 2, … when a task is split around busy blocks
    window: int | None = None  # index of the free window a task segment is in
    key: str = ""  # meeting occurrence key
    title: str = ""

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Overflow:
    plan_item_id: int
    task_id: int
    minutes: int | None  # unscheduled minutes; None for Rest-of-day on a non-working day


@dataclass(frozen=True)
class Capacity:
    planned_min: int  # timed work still to do
    flexible_count: int  # Rest-of-day items
    free_min: int

    @property
    def over_by(self) -> int:
        return max(0, self.planned_min - self.free_min)


@dataclass(frozen=True)
class Schedule:
    segments: tuple[Segment, ...]
    overflow: tuple[Overflow, ...]
    capacity: Capacity
    free_windows: tuple[Interval, ...]
    working: bool
    rest_of_day_min: dict[int, int]  # plan item id → minutes given to a Rest-of-day item

    def task_segments(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.kind == SegmentKind.TASK)

    def segment_at(self, instant: datetime) -> Segment | None:
        """The task segment planned at `instant`, if any."""
        for segment in self.task_segments():
            if segment.start <= instant < segment.end:
                return segment
        return None

    def next_task_after(self, instant: datetime, plan_item_id: int | None) -> Segment | None:
        """The first task segment starting after `instant` for another plan item."""
        for segment in self.task_segments():
            if segment.start >= instant and segment.plan_item_id != plan_item_id:
                return segment
        return None

    def overflowing_ids(self) -> frozenset[int]:
        return frozenset(o.plan_item_id for o in self.overflow)


def _merge(blocks: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for block in sorted(blocks, key=lambda b: (b.start, b.end)):
        if merged and block.start <= merged[-1].end:
            if block.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, block.end)
        else:
            merged.append(block)
    return merged


def _free_windows(window: Interval, busy: list[Interval]) -> list[Interval]:
    free: list[Interval] = []
    cursor = window.start
    for block in busy:
        if block.end <= window.start or block.start >= window.end:
            continue
        if block.start > cursor:
            free.append(Interval(cursor, min(block.start, window.end)))
        cursor = max(cursor, block.end)
    if cursor < window.end:
        free.append(Interval(cursor, window.end))
    return [w for w in free if w.minutes >= MIN_FRAGMENT_MIN]


def _shares(leftover: int, count: int) -> list[int]:
    if count == 0 or leftover <= 0:
        return [0] * count
    base, spare = divmod(leftover, count)
    return [base + (1 if index < spare else 0) for index in range(count)]


def schedule(
    window: Interval | None,
    lunch: Interval | None,
    meetings: Sequence[Busy],
    items: Sequence[PlanInput],
    now: datetime,
) -> Schedule:
    display = [
        Segment(SegmentKind.MEETING, m.start, m.end, key=m.key, title=m.title) for m in meetings
    ]
    if lunch is not None:
        display.append(Segment(SegmentKind.LUNCH, lunch.start, lunch.end))
    flexible = sum(1 for item in items if item.allotted_min is None)
    planned = sum(item.remaining_min or 0 for item in items)

    if window is None:
        # Not a working day: nothing is scheduled and every item overflows (D-015).
        return Schedule(
            segments=tuple(sorted(display, key=lambda s: (s.start, s.kind))),
            overflow=tuple(
                Overflow(item.plan_item_id, item.task_id, item.remaining_min)
                for item in items
                if item.remaining_min is None or item.remaining_min > 0
            ),
            capacity=Capacity(planned, flexible, 0),
            free_windows=(),
            working=False,
            rest_of_day_min={},
        )

    usable = Interval(max(window.start, now), window.end)
    busy = [Interval(m.start, m.end) for m in meetings if m.end > m.start]
    if lunch is not None:
        busy.append(lunch)
    free = _free_windows(usable, _merge(busy)) if usable.start < usable.end else []
    free_min = sum(w.minutes for w in free)

    shares = iter(_shares(free_min - planned, flexible))
    needs: list[tuple[PlanInput, int]] = []
    rest_minutes: dict[int, int] = {}
    for item in items:
        if item.allotted_min is None:
            share = next(shares)
            rest_minutes[item.plan_item_id] = share
            needs.append((item, share))
        else:
            needs.append((item, item.remaining_min or 0))

    segments: list[Segment] = []
    overflow: list[Overflow] = []
    window_index = 0
    cursor = free[0].start if free else usable.end
    for item, total in needs:
        need = total
        part = 0
        while need > 0 and window_index < len(free):
            current = free[window_index]
            cursor = max(cursor, current.start)
            available = int((current.end - cursor).total_seconds() // 60)
            if available <= 0:
                window_index += 1
                continue
            take = min(need, available)
            part += 1
            end = cursor + timedelta(minutes=take)
            segments.append(
                Segment(
                    SegmentKind.TASK,
                    cursor,
                    end,
                    plan_item_id=item.plan_item_id,
                    task_id=item.task_id,
                    part=part,
                    window=window_index,
                )
            )
            cursor = end
            need -= take
            if take == available:
                window_index += 1
        if need > 0 and item.allotted_min is not None:
            overflow.append(Overflow(item.plan_item_id, item.task_id, need))

    return Schedule(
        segments=tuple(sorted(display + segments, key=lambda s: (s.start, s.kind != "task"))),
        overflow=tuple(overflow),
        capacity=Capacity(planned, flexible, free_min),
        free_windows=tuple(free),
        working=True,
        rest_of_day_min=rest_minutes,
    )
