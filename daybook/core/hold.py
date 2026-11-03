"""On-hold rules (SPEC A3, D-046): reasons, follow-ups and how long a task has waited."""

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from gettext import gettext as _

from daybook.core import validation
from daybook.core.models import Hold, HoldReason, Task, TaskStatus


@dataclass(frozen=True)
class HoldRequest:
    reason: HoldReason
    note: str | None = None
    follow_up: date | None = None


def reason_label(reason: HoldReason) -> str:
    """The full wording, used in the hold dialog and the task detail."""
    match reason:
        case HoldReason.BLOCKED:
            return _("Blocked")
        case HoldReason.APPROVAL:
            return _("Waiting for approval or review")
        case HoldReason.SOMEONE:
            return _("Waiting for someone")
        case HoldReason.POSTPONED:
            return _("Postponed")
        case HoldReason.OTHER:
            return _("Other")


def reason_short(reason: HoldReason) -> str:
    """One word for chips: ``on hold · approval · 3d``."""
    match reason:
        case HoldReason.BLOCKED:
            return _("blocked")
        case HoldReason.APPROVAL:
            return _("approval")
        case HoldReason.SOMEONE:
            return _("waiting")
        case HoldReason.POSTPONED:
            return _("postponed")
        case HoldReason.OTHER:
            return _("other")


def validate(request: HoldRequest, today: date) -> HoldRequest:
    return HoldRequest(
        reason=HoldReason(request.reason),
        note=validation.optional_text(request.note, "hold_note"),
        follow_up=validation.follow_up_date(request.follow_up, today),
    )


def days_waiting(hold: Hold, today: date, tz: tzinfo) -> int:
    """Whole local days since the task was put on hold (0 on the same day)."""
    return max(0, (today - hold.since.astimezone(tz).date()).days)


def follow_up_due(task: Task, today: date) -> bool:
    """On or after its follow-up date, an on-hold task resurfaces in "Plan my day"."""
    return (
        task.status == TaskStatus.ON_HOLD
        and task.hold is not None
        and task.hold.follow_up is not None
        and task.hold.follow_up <= today
    )


def started(request: HoldRequest, now: datetime) -> Hold:
    return Hold(reason=request.reason, note=request.note, since=now, follow_up=request.follow_up)
