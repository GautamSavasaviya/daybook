"""A horizontal bar chart: a label, a Cairo-drawn bar and a value per row (SPEC §6).

Only the bars are drawn; labels and values are real widgets, so they follow the system
font size and are read by screen readers. Bars use the accent colour (PLAN §6.2).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import cairo
from gi.repository import Adw, Gtk, Pango

from daybook.services.safe import report_error
from daybook.ui.widgets.trim_tip import tip_if_trimmed

BAR_HEIGHT = 12


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    text: str  # "2h 30m"
    muted: bool = False  # drawn paler (e.g. meetings next to focus time)


class _BarArea(Gtk.DrawingArea):
    def __init__(self, fraction: float, muted: bool) -> None:
        super().__init__(
            content_height=BAR_HEIGHT,
            hexpand=True,
            valign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self._fraction = fraction
        self._muted = muted
        self.set_draw_func(self._draw)

    def _draw(
        self, area: Gtk.DrawingArea, cr: cairo.Context[cairo.Surface], width: int, height: int
    ) -> None:
        try:
            fg = area.get_color()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.1)
            _rounded(cr, 0, 0, width, height)
            cr.fill()
            fill = width * self._fraction
            if fill <= 0:
                return
            accent = Adw.StyleManager.get_default().get_accent_color_rgba()
            alpha = 0.45 if self._muted else 1.0
            cr.set_source_rgba(accent.red, accent.green, accent.blue, alpha)
            _rounded(cr, 0, 0, max(fill, height), height)
            cr.fill()
        except Exception as exc:
            report_error(exc)


def _rounded(cr: cairo.Context[cairo.Surface], x: float, y: float, w: float, h: float) -> None:
    radius = h / 2
    cr.new_sub_path()
    cr.arc(x + w - radius, y + radius, radius, -1.5708, 1.5708)
    cr.arc(x + radius, y + radius, radius, 1.5708, 4.71239)
    cr.close_path()


class BarChart(Gtk.Grid):
    def __init__(self, accessible_name: str) -> None:
        super().__init__(column_spacing=12, row_spacing=8)
        self.update_property([Gtk.AccessibleProperty.LABEL], [accessible_name])

    def set_bars(self, bars: Sequence[Bar]) -> None:
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        peak = max((bar.value for bar in bars), default=0.0)
        for row, bar in enumerate(bars):
            label = tip_if_trimmed(
                Gtk.Label(
                    label=bar.label,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.END,
                    max_width_chars=28,
                    use_markup=False,
                )
            )
            value = Gtk.Label(
                label=bar.text, xalign=1, css_classes=["numeric", "dim-label"], use_markup=False
            )
            fraction = bar.value / peak if peak else 0.0
            self.attach(label, 0, row, 1, 1)
            self.attach(_BarArea(fraction, bar.muted), 1, row, 1, 1)
            self.attach(value, 2, row, 1, 1)
