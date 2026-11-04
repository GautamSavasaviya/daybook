"""Pick how much time a plan item gets: presets, a typed time, or Rest of day (SPEC §2)."""

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Gtk

from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.core.timeutil import fmt_duration
from daybook.services.safe import safe_callback

PRESETS: tuple[int, ...] = (15, 30, 60, 120)


def allotment_label(minutes: int | None) -> str:
    return _("Rest of day") if minutes is None else fmt_duration(minutes)


def short_label(minutes: int | None) -> str:
    """``15m``, ``1h``, ``2h``, ``Day``: for rows of preset buttons."""
    if minutes is None:
        return _("Day")
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return fmt_duration(minutes)


class AllotmentButton(Gtk.MenuButton):
    """Shows an item's time; its popover changes it or removes the item from the plan."""

    def __init__(
        self,
        on_change: Callable[[int | None], None],
        on_remove: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(valign=Gtk.Align.CENTER, css_classes=["flat"])
        self._on_change = on_change
        self._on_remove = on_remove
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)  # fmt: skip
        presets = Gtk.Box(spacing=4, homogeneous=True)
        for minutes in (*PRESETS, None):
            button = Gtk.Button(label=allotment_label(minutes), css_classes=["flat"])
            button.connect("clicked", self._on_preset, minutes)
            presets.append(button)
        box.append(presets)
        self._entry = Gtk.Entry(placeholder_text=_("Other, e.g. 45m or 1h 30m"))
        self._entry.connect("activate", self._on_custom)
        self._error = Gtk.Label(css_classes=["field-error", "caption"], visible=False, wrap=True)
        box.append(self._entry)
        box.append(self._error)
        if on_remove is not None:
            remove = Gtk.Button(
                label=_("Remove From Plan"), css_classes=["flat", "destructive-action"]
            )
            remove.connect("clicked", self._on_remove_clicked)
            box.append(remove)
        self.set_popover(Gtk.Popover(child=box))

    def show_minutes(self, minutes: int | None) -> None:
        label = allotment_label(minutes)
        self.set_label(label)
        self.set_tooltip_text(_("Time today: {time}").format(time=label))
        self.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Time planned: {time}").format(time=label)]
        )

    @safe_callback
    def _on_preset(self, _button: Gtk.Button, minutes: int | None) -> None:
        self.popdown()
        self._on_change(minutes)

    @safe_callback
    def _on_custom(self, entry: Gtk.Entry) -> None:
        try:
            minutes = validation.allotment(validation.parse_duration(entry.get_text(), "allotment"))
        except ValidationError as error:
            self._error.set_text(error.message)
            self._error.set_visible(True)
            return
        if minutes is None:
            return
        self._error.set_visible(False)
        entry.set_text("")
        self.popdown()
        self._on_change(minutes)

    @safe_callback
    def _on_remove_clicked(self, _button: Gtk.Button) -> None:
        self.popdown()
        if self._on_remove is not None:
            self._on_remove()
