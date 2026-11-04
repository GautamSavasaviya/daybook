"""Weekly reports (SPEC §6): hours per project, planned against actual per task, tasks
completed, meeting against focus hours, and how much work was carried over or moved. Pure."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo

from daybook.core import timeutil, tracking
from daybook.core.meetings import effective_end
from daybook.core.models import Occurrence, TimeSlice


@dataclass(frozen=True)
class PlannedItem:
    """One plan item inside the week."""

    day: date
    task_id: int
    label: str
    allotted_min: int | None  # None = Rest of day
    carried: bool  # this item was carried over from an earlier day


@dataclass(frozen=True)
class ProjectHours:
    project_id: int
    name: str
    seconds: int


@dataclass(frozen=True)
class TaskAccuracy:
    task_id: int
    label: str
    planned_min: int  # 0 for Rest of day or unplanned
    actual_min: int
    rest_of_day: bool = False

    @property
    def over_min(self) -> int:
        return self.actual_min - self.planned_min


@dataclass(frozen=True)
class WeeklyReport:
    days: tuple[date, ...]
    per_project: tuple[ProjectHours, ...]
    per_day_s: tuple[int, ...]
    tasks: tuple[TaskAccuracy, ...]
    completed: int
    meeting_min: int  # scheduled meetings
    adhoc_min: int  # "Meeting now", shown separately (D-049)
    focus_min: int  # tracked task time
    carried: int
    planned_tasks: int

    @property
    def tracked_s(self) -> int:
        return sum(self.per_day_s)

    @property
    def empty(self) -> bool:
        return not (self.tracked_s or self.completed or self.meeting_min or self.adhoc_min)


def meeting_minutes(
    occurrences: Sequence[Occurrence], lo: datetime, hi: datetime, now: datetime
) -> tuple[int, int]:
    """(scheduled, ad-hoc) meeting minutes inside [lo, hi), counting no further than now."""
    limit = min(hi, now)
    spans: dict[bool, list[tuple[datetime, datetime]]] = {True: [], False: []}
    for occurrence in occurrences:
        start = max(occurrence.start, lo)
        end = min(effective_end(occurrence, now), limit)
        if end > start:
            spans[occurrence.is_adhoc].append((start, end))
    return _union_seconds(spans[False]) // 60, _union_seconds(spans[True]) // 60


def _union_seconds(spans: list[tuple[datetime, datetime]]) -> int:
    """Seconds covered by the spans, counting overlapping ones once."""
    total = 0.0
    reach: datetime | None = None
    for start, end in sorted(spans):
        if reach is None or start >= reach:
            total += (end - start).total_seconds()
        elif end > reach:
            total += (end - reach).total_seconds()
        reach = end if reach is None else max(reach, end)
    return int(total)


def build(
    days: Sequence[date],
    tz: tzinfo,
    slices: Sequence[TimeSlice],
    plan: Sequence[PlannedItem],
    *,
    completed: int,
    meetings: tuple[int, int],
) -> WeeklyReport:
    bounds = [timeutil.local_day_bounds(day, tz) for day in days]
    per_day = [tracking.clipped_s([(s.start, s.end) for s in slices], lo, hi) for lo, hi in bounds]
    week_lo, week_hi = bounds[0][0], bounds[-1][1]

    project_names: dict[int, str] = {}
    project_s: dict[int, int] = {}
    actual: dict[int, int] = {}
    labels: dict[int, str] = {}
    for item in slices:
        seconds = tracking.overlap_s(item.start, item.end, week_lo, week_hi)
        if not seconds:
            continue
        project_names[item.project_id] = item.project_name
        project_s[item.project_id] = project_s.get(item.project_id, 0) + seconds
        actual[item.task_id] = actual.get(item.task_id, 0) + seconds
        labels.setdefault(
            item.task_id, f"{item.ticket_key} {item.title}" if item.ticket_key else item.title
        )
    per_project = tuple(
        sorted(
            (ProjectHours(pid, project_names[pid], seconds) for pid, seconds in project_s.items()),
            key=lambda p: (-p.seconds, p.name.casefold()),
        )
    )

    planned: dict[int, int] = {}
    rest: set[int] = set()
    carried = 0
    for planned_item in plan:
        labels.setdefault(planned_item.task_id, planned_item.label)
        carried += planned_item.carried
        if planned_item.allotted_min is None:
            rest.add(planned_item.task_id)
        else:
            planned[planned_item.task_id] = (
                planned.get(planned_item.task_id, 0) + planned_item.allotted_min
            )
    tasks = tuple(
        sorted(
            (
                TaskAccuracy(
                    task_id,
                    labels[task_id],
                    planned.get(task_id, 0),
                    actual.get(task_id, 0) // 60,
                    task_id in rest and task_id not in planned,
                )
                for task_id in set(planned) | set(actual) | rest
            ),
            key=lambda t: (-(t.actual_min or t.planned_min), t.label.casefold()),
        )
    )
    return WeeklyReport(
        days=tuple(days),
        per_project=per_project,
        per_day_s=tuple(per_day),
        tasks=tasks,
        completed=completed,
        meeting_min=meetings[0],
        adhoc_min=meetings[1],
        focus_min=sum(per_day) // 60,
        carried=carried,
        planned_tasks=len({p.task_id for p in plan}),
    )
