"""The capacity bar: "Planned 7h 30m / free 5h 45m", and how much won't fit (SPEC §2)."""

from gettext import gettext as _

from gi.repository import Gtk

from daybook.core.planner import Schedule
from daybook.core.timeutil import fmt_duration


class CapacityBar(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        line = Gtk.Box(spacing=12)
        self._label = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["numeric"])
        self._warning = Gtk.Label(xalign=1, css_classes=["warning", "numeric"])
        line.append(self._label)
        line.append(self._warning)
        # A progress bar, not a level bar: it uses the accent colour, not "full = green".
        self._bar = Gtk.ProgressBar()
        self._bar.update_property([Gtk.AccessibleProperty.LABEL], [_("Planned time")])
        self.append(line)
        self.append(self._bar)

    def show_schedule(self, schedule: Schedule) -> None:
        capacity = schedule.capacity
        planned = fmt_duration(capacity.planned_min)
        free = fmt_duration(capacity.free_min)
        if not schedule.working:
            text = _("Not a working day")
        elif capacity.flexible_count:
            text = _("Planned {planned} + rest of day / free {free}").format(
                planned=planned, free=free
            )
        else:
            text = _("Planned {planned} / free {free}").format(planned=planned, free=free)
        self._label.set_text(text)
        over = capacity.over_by
        self._warning.set_visible(over > 0)
        self._warning.set_text(_("⚠ {time} over").format(time=fmt_duration(over)))
        fill = 1.0 if capacity.flexible_count and capacity.free_min else 0.0
        if capacity.free_min:
            fill = max(fill, min(1.0, capacity.planned_min / capacity.free_min))
        self._bar.set_fraction(fill if schedule.working else 0.0)
        if over > 0:
            self._bar.add_css_class("warning")
        else:
            self._bar.remove_css_class("warning")
        self._bar.set_visible(schedule.working)
        description = text if over <= 0 else f"{text}, {self._warning.get_text()}"
        self.update_property([Gtk.AccessibleProperty.LABEL], [description])
