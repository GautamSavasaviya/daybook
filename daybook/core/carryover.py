"""Automatic carry-over of unfinished work (SPEC A5, D-045, PLAN §5.2). Pure.

Every unfinished item planned on an earlier day since the last rollover is added to
today's plan with its remaining time, once per task, from its latest planned day.
"""

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from daybook.core.models import TaskStatus

# Carried items get whole quarter hours, and never less than one (D-045).
CARRY_STEP_MIN = 15


@dataclass(frozen=True)
class SourceItem:
    plan_date: date
    position: int
    task_id: int
    allotted_min: int | None  # None = Rest of day
    carry_count: int
    tracked_min: int  # tracked on the task on `plan_date`
    status: TaskStatus
    deleted: bool


@dataclass(frozen=True)
class CarriedItem:
    task_id: int
    allotted_min: int | None
    carry_count: int
    carried_from: date


def carried_minutes(allotted_min: int, tracked_min: int) -> int:
    """``max(15, ceil_to_15(allotted - tracked))``: even fully tracked work that isn't
    done comes back with a quarter hour."""
    remaining = max(0, allotted_min - tracked_min)
    return max(CARRY_STEP_MIN, math.ceil(remaining / CARRY_STEP_MIN) * CARRY_STEP_MIN)


def rollover(
    sources: Sequence[SourceItem], planned_today: Collection[int], today: date
) -> list[CarriedItem]:
    """Items to append to today's plan, in their original order (by day, then position).

    Done, on-hold and deleted tasks stay where they were, as do tasks already planned
    for today. Sources dated today or later are ignored.
    """
    latest: dict[int, SourceItem] = {}
    for item in sources:
        if item.plan_date >= today:
            continue
        current = latest.get(item.task_id)
        if current is None or item.plan_date > current.plan_date:
            latest[item.task_id] = item
    carried: list[CarriedItem] = []
    for item in sorted(latest.values(), key=lambda s: (s.plan_date, s.position, s.task_id)):
        unfinished = not item.deleted and item.status in (
            TaskStatus.TO_DO,
            TaskStatus.IN_PROGRESS,
        )
        if not unfinished or item.task_id in planned_today:
            continue
        allotted = (
            None
            if item.allotted_min is None
            else carried_minutes(item.allotted_min, item.tracked_min)
        )
        carried.append(
            CarriedItem(
                task_id=item.task_id,
                allotted_min=allotted,
                carry_count=item.carry_count + 1,
                carried_from=item.plan_date,
            )
        )
    return carried
