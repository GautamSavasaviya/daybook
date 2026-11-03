"""A button showing an optional date; its popover has a calendar and quick choices."""

from collections.abc import Callable
from datetime import date, timedelta
from gettext import gettext as _

from gi.repository import GLib, Gtk

from daybook.core.timeutil import fmt_date, fmt_day
from daybook.services.safe import safe_callback


def capitalised_day(day: date, today: date) -> str:
    text = fmt_day(day, today)
    return text[:1].upper() + text[1:]


class DateButton(Gtk.MenuButton):
    def __init__(
        self,
        *,
        today: Callable[[], date],
        empty_label: str,
        accessible_name: str,
        allow_clear: bool = True,
        on_changed: Callable[[date | None], None] | None = None,
    ) -> None:
        super().__init__(valign=Gtk.Align.CENTER, tooltip_text=accessible_name)
        self._today = today
        self._empty_label = empty_label
        self._accessible_name = accessible_name
        self._on_changed = on_changed
        self._value: date | None = None
        self._setting = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._calendar = Gtk.Calendar()
        self._calendar.connect("day-selected", self._on_day_selected)
        box.append(self._calendar)
        quick = Gtk.Box(spacing=6, homogeneous=True)
        for label, offset in ((_("Today"), 0), (_("Tomorrow"), 1)):
            button = Gtk.Button(label=label)
            button.connect("clicked", self._on_quick, offset)
            quick.append(button)
        if allow_clear:
            clear = Gtk.Button(label=_("Clear"))
            clear.connect("clicked", self._on_clear)
            quick.append(clear)
        box.append(quick)
        popover = Gtk.Popover(child=box)
        popover.connect("show", self._on_popover_show)
        self.set_popover(popover)
        self._refresh_label()

    @property
    def value(self) -> date | None:
        return self._value

    def set_value(self, value: date | None) -> None:
        """Show `value` without reporting a change."""
        self._value = value
        self._refresh_label()

    def _refresh_label(self) -> None:
        if self._value is None:
            self.set_label(self._empty_label)
            description = _("{name}: none").format(name=self._accessible_name)
        else:
            self.set_label(capitalised_day(self._value, self._today()))
            description = f"{self._accessible_name}: {fmt_date(self._value)}"
        self.update_property([Gtk.AccessibleProperty.LABEL], [description])

    def _choose(self, value: date | None) -> None:
        self._value = value
        self._refresh_label()
        self.popdown()
        if self._on_changed is not None:
            self._on_changed(value)

    @safe_callback
    def _on_popover_show(self, _popover: Gtk.Popover) -> None:
        shown = self._value or self._today()
        self._setting = True
        try:
            moment = GLib.DateTime.new_utc(shown.year, shown.month, shown.day, 12, 0, 0)
            assert moment is not None
            self._calendar.set_date(moment)
        finally:
            self._setting = False

    @safe_callback
    def _on_day_selected(self, calendar: Gtk.Calendar) -> None:
        if self._setting:
            return
        picked = calendar.get_date()
        self._choose(date(picked.get_year(), picked.get_month(), picked.get_day_of_month()))

    @safe_callback
    def _on_quick(self, _button: Gtk.Button, offset: int) -> None:
        self._choose(self._today() + timedelta(days=offset))

    @safe_callback
    def _on_clear(self, _button: Gtk.Button) -> None:
        self._choose(None)
