""" "Move to…" (SPEC A4, D-047): where a task can be moved and how the move is recorded."""

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from gettext import gettext as _

from daybook.core import validation
from daybook.core.calendar import WorkCalendar
from daybook.core.errors import ValidationError
from daybook.core.timeutil import fmt_date, fmt_day, fmt_day_month

# Allotment for a task planned without one, unless it has an estimate (D-021).
DEFAULT_ALLOTMENT_MIN = 30


class MoveKind(StrEnum):
    TOMORROW = "tomorrow"
    NEXT_WORKING_DAY = "next_working_day"
    DATE = "date"
    UNPLANNED = "unplanned"


@dataclass(frozen=True)
class MoveChoice:
    kind: MoveKind
    day: date | None = None  # only for MoveKind.DATE


def quick_choice(today: date, calendar: WorkCalendar) -> MoveChoice:
    """The one-click option: "Tomorrow" when tomorrow is a working day, otherwise "Next
    working day" (D-069). Both mean the next working day, so only one is ever offered."""
    if calendar.is_working(today + timedelta(days=1)):
        return MoveChoice(MoveKind.TOMORROW)
    return MoveChoice(MoveKind.NEXT_WORKING_DAY)


def resolve(choice: MoveChoice, today: date, calendar: WorkCalendar) -> date | None:
    """The plan date a choice moves to; None means unplanned.

    Raises ValidationError("move_date", …) for a past date or a non-working day, naming the
    next working day in the message.
    """
    match choice.kind:
        case MoveKind.TOMORROW | MoveKind.NEXT_WORKING_DAY:
            return calendar.next_working_day(today)
        case MoveKind.UNPLANNED:
            return None
        case MoveKind.DATE:
            if choice.day is None:
                raise ValidationError("move_date", _("Pick a date."))
            if choice.day < today:
                raise ValidationError("move_date", _("Pick today or a later date."))
            if not calendar.is_working(choice.day):
                suggestion = calendar.next_working_day(choice.day)
                raise ValidationError(
                    "move_date",
                    _("{day} isn't a working day. The next one is {next}.").format(
                        day=fmt_date(choice.day), next=fmt_date(suggestion)
                    ),
                )
            return choice.day


def note_line(today: date, target: date | None, reason: str | None) -> str | None:
    """The dated line a move with a reason adds to the task's notes (D-047), or None."""
    cleaned = validation.optional_text(reason, "move_reason")
    if cleaned is None:
        return None
    where = _("unplanned") if target is None else fmt_day(target, today)
    return _("{stamp}: moved to {where} — {reason}").format(
        stamp=fmt_day_month(today), where=where, reason=cleaned
    )


def append_note(notes: str, line: str | None) -> str:
    if line is None:
        return notes
    return f"{notes.rstrip()}\n{line}" if notes.strip() else line


def default_allotment(estimate_min: int | None) -> int:
    """Minutes to plan a task for when the user didn't say (D-021): its estimate, or 30 min.

    An estimate longer than a day is capped at 24 h, the largest allotment allowed."""
    if estimate_min:
        return min(estimate_min, validation.MAX_ALLOTMENT_MIN)
    return DEFAULT_ALLOTMENT_MIN
