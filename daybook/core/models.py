"""Domain models: frozen dataclasses and enums shared by every layer.

Rows are turned into these by the repositories; nothing else builds them from SQL.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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


class RecurrenceFreq(StrEnum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"  # Monday to Friday, whatever the workdays setting (D-012)
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class Recurrence:
    """How a meeting or task repeats (PLAN §4). `days` are weekdays, 0 = Monday, used by
    WEEKLY only (empty = the anchor's weekday). `until` is an inclusive local date."""

    freq: RecurrenceFreq
    interval: int = 1
    days: frozenset[int] = frozenset()
    until: date | None = None


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
    recurrence: Recurrence | None = None
    recurs_from_id: int | None = None


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
    recurrence: Recurrence | None = None


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
    today: int = 0  # open tasks in today's plan


@dataclass(frozen=True)
class Tracked:
    """Seconds tracked on a task: today (clipped to the local day) and in total."""

    today_s: int
    total_s: int


@dataclass(frozen=True)
class TimeSlice:
    """A stretch of tracked time with its task, for timesheets and reports (UTC)."""

    task_id: int
    ticket_key: str | None
    title: str
    project_id: int
    project_name: str
    start: datetime
    end: datetime


class EntrySource(StrEnum):
    TIMER = "timer"
    MANUAL = "manual"


@dataclass(frozen=True)
class TimeEntry:
    """A closed stretch of tracked time (UTC, half-open). Entries never overlap (D-023)."""

    id: int
    task_id: int
    start: datetime
    end: datetime
    note: str
    source: EntrySource

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


class TimerRun(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"


class PauseReason(StrEnum):
    USER = "user"
    MEETING = "meeting"
    IDLE = "idle"


@dataclass(frozen=True)
class TimerState:
    """The live timer (the singleton `timer_state` row). No row means no timer. A running
    segment becomes a time entry only when it is closed (D-023)."""

    task_id: int
    run: TimerRun
    segment_start: datetime | None  # set iff running
    last_seen: datetime  # heartbeat
    pause_reason: PauseReason | None = None
    paused_by_meeting_key: str | None = None  # the occurrence that paused it

    @property
    def running(self) -> bool:
        return self.run == TimerRun.RUNNING


@dataclass(frozen=True)
class TimeOff:
    day: date
    part: TimeOffPart
    note: str = ""


@dataclass(frozen=True)
class Meeting:
    """A meeting series (or a one-off). `start` is the first occurrence (the anchor);
    occurrences repeat in the wall-clock time of `tzid` (D-010)."""

    id: int
    title: str
    start: datetime
    duration_min: int
    tzid: str
    link: str | None
    recurrence: Recurrence | None
    reminder_offsets: tuple[int, ...]
    project_id: int | None
    is_adhoc: bool
    open_ended: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class MeetingDraft:
    title: str
    start: datetime
    duration_min: int
    link: str | None = None
    recurrence: Recurrence | None = None
    reminder_offsets: tuple[int, ...] = (10, 1)
    project_id: int | None = None


@dataclass(frozen=True)
class MeetingException:
    """A change to one occurrence, identified by its original start (PLAN §5.4).
    None fields inherit from the series."""

    id: int
    meeting_id: int
    original_start: datetime
    cancelled: bool = False
    title: str | None = None
    start: datetime | None = None
    duration_min: int | None = None
    link: str | None = None
    reminder_offsets: tuple[int, ...] | None = None


@dataclass(frozen=True)
class OccurrenceEdit:
    """New values for one occurrence ("This occurrence" in the editor)."""

    title: str
    start: datetime
    duration_min: int
    link: str | None
    reminder_offsets: tuple[int, ...]


@dataclass(frozen=True)
class Occurrence:
    meeting_id: int
    original_start: datetime  # identity, with meeting_id (PLAN §5.4)
    start: datetime
    duration_min: int
    title: str
    link: str | None
    reminder_offsets: tuple[int, ...]
    project_id: int | None
    recurring: bool
    is_adhoc: bool = False
    open_ended: bool = False
    edited: bool = False  # has a single-occurrence change

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)

    @property
    def key(self) -> str:
        """``{meeting_id}:{original start}``, used by reminder keys and exceptions."""
        return occurrence_key(self.meeting_id, self.original_start)


def occurrence_key(meeting_id: int, original_start: datetime) -> str:
    return f"{meeting_id}:{original_start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
