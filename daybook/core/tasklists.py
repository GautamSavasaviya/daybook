"""What each task list contains and how it is grouped (D-051).

The repository turns a `TaskQuery` into SQL; `sections()` splits the sorted result into the
groups the list shows. Both are pure apart from the query itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from enum import StrEnum

from daybook.core.models import Priority, TaskStatus, TaskSummary

UPCOMING_DAYS = 14


class ListKind(StrEnum):
    INBOX = "inbox"
    UPCOMING = "upcoming"
    PROJECT = "project"
    DONE = "done"
    SEARCH = "search"


@dataclass(frozen=True)
class TaskFilter:
    """Narrows any list. None means "any"."""

    project_id: int | None = None
    status: TaskStatus | None = None
    priority: Priority | None = None
    tag: str | None = None
    requested_by: str | None = None

    @property
    def active(self) -> bool:
        return any(
            value is not None
            for value in (
                self.project_id,
                self.status,
                self.priority,
                self.tag,
                self.requested_by,
            )
        )


@dataclass(frozen=True)
class TaskQuery:
    kind: ListKind
    today: date
    filter: TaskFilter = TaskFilter()
    project_id: int | None = None  # the project shown by a PROJECT list
    text: str = ""  # SEARCH only

    @property
    def horizon(self) -> date:
        """First day after the Upcoming window."""
        return self.today + timedelta(days=UPCOMING_DAYS)


class SectionKind(StrEnum):
    ALL = "all"  # an ungrouped list
    OVERDUE = "overdue"
    DAY = "day"
    ON_HOLD = "on_hold"
    IN_PROGRESS = "in_progress"
    TO_DO = "todo"
    OPEN = "open"
    DONE = "done"


@dataclass(frozen=True)
class Section:
    kind: SectionKind
    items: tuple[TaskSummary, ...]
    day: date | None = None  # DAY sections, and DONE sections of the Done list


def sections(
    kind: ListKind, items: Sequence[TaskSummary], today: date, tz: tzinfo
) -> tuple[Section, ...]:
    """Group a query result. Items keep their query order within a section."""
    match kind:
        case ListKind.INBOX:
            return _non_empty([Section(SectionKind.ALL, tuple(items))])
        case ListKind.UPCOMING:
            return _upcoming(items, today)
        case ListKind.PROJECT:
            return _by_status(items)
        case ListKind.DONE:
            return _done_by_day(items, tz)
        case ListKind.SEARCH:
            open_items = tuple(s for s in items if s.task.status != TaskStatus.DONE)
            done_items = tuple(s for s in items if s.task.status == TaskStatus.DONE)
            return _non_empty(
                [Section(SectionKind.OPEN, open_items), Section(SectionKind.DONE, done_items)]
            )


def upcoming_day(summary: TaskSummary, today: date) -> date | None:
    """The day a task is listed under in Upcoming: the earlier of its due date and its next
    plan date, within the window. None if it is overdue or on hold."""
    task = summary.task
    if task.status == TaskStatus.ON_HOLD:
        return None
    if task.due_date is not None and task.due_date < today:
        return None
    horizon = today + timedelta(days=UPCOMING_DAYS)
    candidates = [
        day
        for day in (task.due_date, summary.next_plan.day if summary.next_plan else None)
        if day is not None and today <= day < horizon
    ]
    return min(candidates) if candidates else None


def _upcoming(items: Sequence[TaskSummary], today: date) -> tuple[Section, ...]:
    overdue: list[TaskSummary] = []
    days: dict[date, list[TaskSummary]] = {}
    on_hold: list[TaskSummary] = []
    for summary in items:
        task = summary.task
        if task.status == TaskStatus.ON_HOLD:
            on_hold.append(summary)
        elif task.due_date is not None and task.due_date < today:
            overdue.append(summary)
        else:
            day = upcoming_day(summary, today)
            if day is not None:
                days.setdefault(day, []).append(summary)
    # On hold: soonest follow-up first, then those without one (D-051).
    on_hold.sort(
        key=lambda s: (
            s.task.hold is None or s.task.hold.follow_up is None,
            s.task.hold.follow_up if s.task.hold and s.task.hold.follow_up else date.max,
        )
    )
    result = [Section(SectionKind.OVERDUE, tuple(overdue))]
    result += [Section(SectionKind.DAY, tuple(days[day]), day) for day in sorted(days)]
    result.append(Section(SectionKind.ON_HOLD, tuple(on_hold)))
    return _non_empty(result)


def _by_status(items: Sequence[TaskSummary]) -> tuple[Section, ...]:
    order = (
        (TaskStatus.IN_PROGRESS, SectionKind.IN_PROGRESS),
        (TaskStatus.TO_DO, SectionKind.TO_DO),
        (TaskStatus.ON_HOLD, SectionKind.ON_HOLD),
        (TaskStatus.DONE, SectionKind.DONE),
    )
    return _non_empty(
        [
            Section(kind, tuple(s for s in items if s.task.status == status))
            for status, kind in order
        ]
    )


def _done_by_day(items: Sequence[TaskSummary], tz: tzinfo) -> tuple[Section, ...]:
    groups: dict[date, list[TaskSummary]] = {}
    for summary in items:
        completed = summary.task.completed_at
        if completed is None:  # pragma: no cover — the schema guarantees it for done tasks
            continue
        groups.setdefault(completed.astimezone(tz).date(), []).append(summary)
    return tuple(
        Section(SectionKind.DONE, tuple(groups[day]), day) for day in sorted(groups, reverse=True)
    )


def _non_empty(result: list[Section]) -> tuple[Section, ...]:
    return tuple(section for section in result if section.items)
