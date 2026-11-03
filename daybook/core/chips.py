"""The small labels on task rows (PLAN §6.2): which ones a task gets and how they read.

The UI only draws them; the rules (when a due date counts as overdue, when "moved" starts
showing, …) live here so every list agrees.
"""

from dataclasses import dataclass
from datetime import date, tzinfo
from enum import StrEnum
from gettext import gettext as _
from gettext import ngettext

from daybook.core import hold as hold_rules
from daybook.core.models import Priority, TaskStatus, TaskSummary
from daybook.core.timeutil import fmt_day, fmt_duration

# "moved ×N" appears from the second move (D-047): one reschedule is normal.
MOVED_CHIP_FROM = 2


class ChipKind(StrEnum):
    PRIORITY = "priority"
    OVERDUE = "overdue"
    DUE_TODAY = "due_today"
    DUE = "due"
    ON_HOLD = "on_hold"
    FOLLOW_UP = "follow_up"
    CARRIED = "carried"
    MOVED = "moved"
    PLANNED = "planned"
    REQUESTED_BY = "requested_by"
    CHECKLIST = "checklist"
    TAG = "tag"
    DONE = "done"


class ChipStyle(StrEnum):
    """Maps to libadwaita named colours in the UI."""

    ERROR = "error"
    WARNING = "warning"
    ACCENT = "accent"
    SUCCESS = "success"
    NEUTRAL = "neutral"
    DIM = "dim"


@dataclass(frozen=True)
class Chip:
    kind: ChipKind
    label: str
    description: str  # accessible text: chips never rely on colour alone (PLAN §6.4)
    style: ChipStyle = ChipStyle.NEUTRAL
    person: str = ""  # REQUESTED_BY: the name, for the initials avatar


def task_chips(summary: TaskSummary, today: date, tz: tzinfo) -> tuple[Chip, ...]:
    """Chips in display order. The project and ticket key are shown by the row itself."""
    task = summary.task
    chips: list[Chip] = []
    if task.priority != Priority.P3:
        style = {
            Priority.P1: ChipStyle.ERROR,
            Priority.P2: ChipStyle.WARNING,
            Priority.P4: ChipStyle.DIM,
        }[task.priority]
        chips.append(
            Chip(
                ChipKind.PRIORITY,
                f"P{task.priority.value}",
                _("Priority {n}").format(n=task.priority.value),
                style,
            )
        )
    if task.status == TaskStatus.DONE:
        if task.completed_at is not None:
            day = fmt_day(task.completed_at.astimezone(tz).date(), today)
            label = _("done {day}").format(day=day)
        else:  # pragma: no cover — the schema guarantees completed_at for done tasks
            label = _("done")
        chips.append(Chip(ChipKind.DONE, label, label, ChipStyle.SUCCESS))
    if task.hold is not None:
        days = hold_rules.days_waiting(task.hold, today, tz)
        reason = hold_rules.reason_short(task.hold.reason)
        label = _("on hold · {reason} · {days}d").format(reason=reason, days=days)
        description = ngettext(
            "On hold: {reason}, waiting {days} day",
            "On hold: {reason}, waiting {days} days",
            days,
        ).format(reason=hold_rules.reason_label(task.hold.reason), days=days)
        chips.append(Chip(ChipKind.ON_HOLD, label, description, ChipStyle.WARNING))
        if hold_rules.follow_up_due(task, today):
            chips.append(
                Chip(ChipKind.FOLLOW_UP, _("follow up"), _("Follow-up is due"), ChipStyle.ACCENT)
            )
    if task.due_date is not None and task.status != TaskStatus.DONE:
        chips.append(_due_chip(task.due_date, today))
    plan = summary.next_plan
    if plan is not None and task.status != TaskStatus.DONE:
        if plan.day == today and plan.carry_count > 0:
            chips.append(
                Chip(
                    ChipKind.CARRIED,
                    _("carried ×{n}").format(n=plan.carry_count),
                    ngettext(
                        "Carried over {n} day", "Carried over {n} days", plan.carry_count
                    ).format(n=plan.carry_count),
                    ChipStyle.ACCENT,
                )
            )
        amount = _("rest of day") if plan.allotted_min is None else fmt_duration(plan.allotted_min)
        when = fmt_day(plan.day, today)
        chips.append(
            Chip(
                ChipKind.PLANNED,
                f"{when} · {amount}",
                _("Planned {when}, {amount}").format(when=when, amount=amount),
                ChipStyle.ACCENT if plan.day == today else ChipStyle.NEUTRAL,
            )
        )
    if task.moved_count >= MOVED_CHIP_FROM:
        chips.append(
            Chip(
                ChipKind.MOVED,
                _("moved ×{n}").format(n=task.moved_count),
                _("Moved {n} times").format(n=task.moved_count),
                ChipStyle.WARNING,
            )
        )
    if task.requested_by:
        chips.append(
            Chip(
                ChipKind.REQUESTED_BY,
                _("from {name}").format(name=task.requested_by),
                _("Requested by {name}").format(name=task.requested_by),
                person=task.requested_by,
            )
        )
    if summary.checklist_total:
        chips.append(
            Chip(
                ChipKind.CHECKLIST,
                f"{summary.checklist_done}/{summary.checklist_total}",
                _("{done} of {total} checklist items done").format(
                    done=summary.checklist_done, total=summary.checklist_total
                ),
                ChipStyle.DIM,
            )
        )
    chips.extend(
        Chip(ChipKind.TAG, f"#{tag}", _("Tag {tag}").format(tag=tag), ChipStyle.DIM)
        for tag in summary.tags
    )
    return tuple(chips)


def _due_chip(due: date, today: date) -> Chip:
    days = (today - due).days
    if days > 0:
        return Chip(
            ChipKind.OVERDUE,
            _("overdue {days}d").format(days=days),
            ngettext("Overdue by {days} day", "Overdue by {days} days", days).format(days=days),
            ChipStyle.ERROR,
        )
    if days == 0:
        return Chip(ChipKind.DUE_TODAY, _("due today"), _("Due today"), ChipStyle.WARNING)
    when = fmt_day(due, today)
    return Chip(ChipKind.DUE, _("due {day}").format(day=when), _("Due {day}").format(day=when))
