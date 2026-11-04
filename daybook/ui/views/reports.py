"""Reports (D-051): the Timesheet, the Standup and the weekly report in one place."""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.services.context import AppContext
from daybook.services.safe import safe_callback
from daybook.ui.views.standup import StandupPane
from daybook.ui.views.timesheet import TimesheetPane
from daybook.ui.views.weekly import WeeklyPane


class ReportsView(Gtk.Box):
    def __init__(self, context: AppContext) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)
        self._stack.add_named(TimesheetPane(context), "timesheet")
        self._stack.add_named(StandupPane(context), "standup")
        self._stack.add_named(WeeklyPane(context), "week")

        switch = Adw.ToggleGroup(halign=Gtk.Align.CENTER, can_shrink=True)
        for name, label in (
            ("timesheet", _("Timesheet")),
            ("standup", _("Standup")),
            ("week", _("Week")),
        ):
            switch.add(Adw.Toggle(name=name, label=label))
        switch.set_active_name("timesheet")
        switch.connect("notify::active-name", self._on_switched)
        for widget in (switch, self._stack):
            widget.set_margin_start(12)
            widget.set_margin_end(12)
        switch.set_margin_top(12)
        self.append(switch)
        self.append(Adw.Clamp(child=self._stack, maximum_size=1000, vexpand=True))

    @safe_callback
    def _on_switched(self, group: Adw.ToggleGroup, _param: object) -> None:
        self._stack.set_visible_child_name(group.get_active_name() or "timesheet")
