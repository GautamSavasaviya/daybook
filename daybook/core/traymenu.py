"""The tray menu and the text beside the icon (D-032). Pure: the D-Bus side only encodes it."""

from dataclasses import dataclass
from enum import StrEnum
from gettext import gettext as _

from daybook.core.timeutil import fmt_duration


class Command(StrEnum):
    SHOW = "show"
    QUICK_ADD = "quick-add"
    MEETING_NOW = "meeting-now"
    TOGGLE_TIMER = "toggle-timer"
    QUIT = "quit"


@dataclass(frozen=True)
class Entry:
    id: int
    label: str
    command: Command | None  # None: a separator
    enabled: bool = True


@dataclass(frozen=True)
class TrayStatus:
    """What the tray shows for the timer."""

    task: str | None  # "PROJ-123 Fix login", None: no timer
    running: bool
    left_min: int | None  # of today's allotment; None: none or Rest of day
    tracked_min: int = 0


def title(status: TrayStatus) -> str:
    if status.task is None:
        return _("Daybook")
    return status.task


def tooltip(status: TrayStatus) -> str:
    """ "PROJ-123 Fix login · 23m left · running", one line, like `daybook --status`."""
    if status.task is None:
        return _("No timer running")
    if status.left_min is None:
        amount = _("{time} today").format(time=fmt_duration(status.tracked_min))
    elif status.left_min >= 0:
        amount = _("{time} left").format(time=fmt_duration(status.left_min))
    else:
        amount = _("{time} over").format(time=fmt_duration(-status.left_min))
    state = _("running") if status.running else _("paused")
    return f"{status.task} · {amount} · {state}"


def menu(status: TrayStatus) -> tuple[Entry, ...]:
    """Show, Quick add, Meeting now, Pause/Resume (or Start when nothing is timed), Quit."""
    if status.task is None:
        toggle = Entry(4, _("Pause Timer"), Command.TOGGLE_TIMER, enabled=False)
    elif status.running:
        toggle = Entry(4, _("Pause Timer"), Command.TOGGLE_TIMER)
    else:
        toggle = Entry(4, _("Resume Timer"), Command.TOGGLE_TIMER)
    return (
        Entry(1, _("Show Daybook"), Command.SHOW),
        Entry(2, _("Quick Add…"), Command.QUICK_ADD),
        Entry(3, _("Meeting Now…"), Command.MEETING_NOW),
        toggle,
        Entry(5, "", None),
        Entry(6, _("Quit"), Command.QUIT),
    )


def command_for(entry_id: int, status: TrayStatus) -> Command | None:
    for entry in menu(status):
        if entry.id == entry_id and entry.enabled:
            return entry.command
    return None
