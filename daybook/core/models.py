"""Domain models: frozen dataclasses and enums shared by every layer.

Rows are turned into these by the repositories; nothing else builds them from SQL.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum, StrEnum

# Seeded by migration 0001 (D-043): the default area, renameable but never deleted.
GENERAL_PROJECT_ID = 1


class ProjectColor(StrEnum):
    """The fixed palette (D-019); each maps to a libadwaita accent colour."""

    BLUE = "blue"
    TEAL = "teal"
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"
    PINK = "pink"
    PURPLE = "purple"
    SLATE = "slate"


class Priority(IntEnum):
    P1 = 1
    P2 = 2
    P3 = 3
    P4 = 4


DEFAULT_PRIORITY = Priority.P3


class TaskStatus(StrEnum):
    TO_DO = "todo"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    DONE = "done"


ACTIVE_STATUSES = frozenset({TaskStatus.TO_DO, TaskStatus.IN_PROGRESS})


class HoldReason(StrEnum):
    BLOCKED = "blocked"
    APPROVAL = "approval"
    SOMEONE = "someone"
    POSTPONED = "postponed"
    OTHER = "other"


class TimeOffPart(StrEnum):
    FULL = "full"
    AM = "am"
    PM = "pm"


@dataclass(frozen=True)
class Project:
    """A project, or an *area* when it has no ticket prefix and no URL template (D-043)."""

    id: int
    name: str
    color: ProjectColor
    key_prefix: str | None
    url_template: str | None
    archived: bool
    sort_order: int
    created_at: datetime

    @property
    def is_area(self) -> bool:
        return self.key_prefix is None and self.url_template is None

    @property
    def is_general(self) -> bool:
        return self.id == GENERAL_PROJECT_ID


@dataclass(frozen=True)
class ProjectDraft:
    """The editable fields of a project, as typed by the user (validated before saving)."""

    name: str
    color: ProjectColor = ProjectColor.BLUE
    key_prefix: str | None = None
    url_template: str | None = None


@dataclass(frozen=True)
class Hold:
    reason: HoldReason
    note: str | None
    since: datetime
    follow_up: date | None


@dataclass(frozen=True)
class Task:
    id: int
    project_id: int
    title: str
    ticket_key: str | None
    requested_by: str | None
    priority: Priority
    status: TaskStatus
    hold: Hold | None
    estimate_min: int | None
    due_date: date | None
    notes: str
    moved_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True)
class PlanRequest:
    """Plan a task on `day`; `allotted_min=None` means "Rest of day" (D-050)."""

    day: date
    allotted_min: int | None


@dataclass(frozen=True)
class TaskDraft:
    """A new task. Only the title is required (SPEC A1)."""

    title: str
    project_id: int = GENERAL_PROJECT_ID
    priority: Priority = DEFAULT_PRIORITY
    ticket_key: str | None = None
    requested_by: str | None = None
    estimate_min: int | None = None
    due_date: date | None = None
    notes: str = ""
    tags: tuple[str, ...] = ()
    plan: PlanRequest | None = None


@dataclass(frozen=True)
class ChecklistItem:
    id: int
    task_id: int
    text: str
    done: bool
    position: int


@dataclass(frozen=True)
class PlanItem:
    id: int
    plan_date: date
    task_id: int
    allotted_min: int | None  # None = Rest of day
    position: int
    carry_count: int
    carried_from: date | None


@dataclass(frozen=True)
class PlanSlot:
    """A task's next plan item on or after today, as shown in task rows."""

    day: date
    allotted_min: int | None
    carry_count: int


@dataclass(frozen=True)
class TaskSummary:
    """Everything a task row shows, loaded in one query."""

    task: Task
    project_name: str
    project_color: ProjectColor
    project_archived: bool
    tags: tuple[str, ...] = ()
    next_plan: PlanSlot | None = None
    checklist_done: int = 0
    checklist_total: int = 0


@dataclass(frozen=True)
class SidebarCounts:
    inbox: int
    upcoming: int
    per_project: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Tracked:
    """Seconds tracked on a task: today (clipped to the local day) and in total."""

    today_s: int
    total_s: int
