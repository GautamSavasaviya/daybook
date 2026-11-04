"""The standup (SPEC §6): Yesterday / Today / Blockers as plain text or Markdown. Pure.

"Yesterday" is the last *worked* day (the calendar skips weekends and leave, so on Monday it
is Friday). Direct requests read "for Rahul" (D-044); blockers are On-hold tasks with their
reason and how long they have waited (D-046).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from gettext import gettext as _

from daybook.core import timeutil
from daybook.core.timeutil import fmt_duration


@dataclass(frozen=True)
class Worked:
    name: str  # ticket key and title
    seconds: int
    done: bool
    requested_by: str | None = None


@dataclass(frozen=True)
class Planned:
    name: str
    allotted_min: int | None  # None = Rest of day
    requested_by: str | None = None


@dataclass(frozen=True)
class Blocker:
    name: str
    reason: str  # "Waiting for approval or review"
    days: int
    note: str | None = None


@dataclass(frozen=True)
class Standup:
    last_day: date
    today: date
    yesterday: tuple[str, ...]
    plan: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def yesterday_heading(self) -> str:
        if (self.today - self.last_day).days == 1:
            return _("Yesterday")
        return _("Yesterday ({day})").format(day=timeutil.fmt_date(self.last_day))

    def sections(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        none = (_("Nothing"),)
        return (
            (self.yesterday_heading, self.yesterday or none),
            (_("Today"), self.plan or none),
            (_("Blockers"), self.blockers or (_("None"),)),
        )


def _for(name: str | None) -> str:
    return " " + _("(for {name})").format(name=name) if name else ""


def build(
    worked: Sequence[Worked],
    plan: Sequence[Planned],
    blockers: Sequence[Blocker],
    *,
    last_day: date,
    today: date,
) -> Standup:
    yesterday = []
    for item in sorted(worked, key=lambda w: (not w.done, -w.seconds, w.name.casefold())):
        bits = [_("done")] if item.done else []
        if item.seconds >= 60:
            bits.append(fmt_duration(item.seconds // 60))
        detail = f" — {', '.join(bits)}" if bits else ""
        yesterday.append(f"{item.name}{_for(item.requested_by)}{detail}")
    today_lines = []
    for planned in plan:
        amount = (
            _("rest of day") if planned.allotted_min is None else fmt_duration(planned.allotted_min)
        )
        today_lines.append(f"{planned.name}{_for(planned.requested_by)} — {amount}")
    blocked = []
    for block in blockers:
        waiting = _("waiting {n}d").format(n=block.days) if block.days else _("since today")
        line = f"{block.name} — {block.reason.lower()}, {waiting}"
        if block.note:
            line += f" ({block.note})"
        blocked.append(line)
    return Standup(last_day, today, tuple(yesterday), tuple(today_lines), tuple(blocked))


def to_text(standup: Standup) -> str:
    lines: list[str] = []
    for heading, items in standup.sections():
        lines.append(f"{heading}:")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def to_markdown(standup: Standup) -> str:
    lines: list[str] = []
    for heading, items in standup.sections():
        lines.append(f"**{heading}**")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
