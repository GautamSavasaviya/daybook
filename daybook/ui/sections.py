"""The fixed sidebar sections, in Ctrl+1…7 order (D-051)."""

from dataclasses import dataclass
from gettext import gettext as _


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    icon: str
    empty_title: str
    empty_description: str


def sections() -> tuple[Section, ...]:
    # Built on call so the strings are translated after gettext is configured.
    return (
        Section(
            "today",
            _("Today"),
            "weather-clear-symbolic",
            _("Nothing Planned Yet"),
            _("Your timeline, the task you're working on and your next meeting will appear here."),
        ),
        Section(
            "plan",
            _("Plan"),
            "view-list-ordered-symbolic",
            _("Plan Your Day"),
            _(
                "Pick tasks for the day and give each one a time. Daybook fits them around "
                "your meetings."
            ),
        ),
        Section(
            "inbox",
            _("Inbox"),
            "daybook-inbox-symbolic",
            _("Inbox Is Empty"),
            _("Tasks you haven't planned yet collect here."),
        ),
        Section(
            "upcoming",
            _("Upcoming"),
            "x-office-calendar-symbolic",
            _("Nothing Coming Up"),
            _("Tasks due or planned in the next two weeks, and tasks on hold, appear here."),
        ),
        Section(
            "meetings",
            _("Meetings"),
            "camera-video-symbolic",
            _("No Meetings"),
            _("Your meetings and their reminders appear here."),
        ),
        Section(
            "reports",
            _("Reports"),
            "daybook-reports-symbolic",
            _("No Reports Yet"),
            _(
                "Time per project, planned versus actual, your standup and your timesheet "
                "appear here once you track time."
            ),
        ),
        Section(
            "done",
            _("Done"),
            "object-select-symbolic",
            _("Nothing Done Yet"),
            _("Completed tasks appear here and stay searchable."),
        ),
    )
