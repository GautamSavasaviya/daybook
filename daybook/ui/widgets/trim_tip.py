"""Hover tooltips for text that was cut short (D-118): wherever a label ellipsizes to keep
the layout tidy, hovering it (or focusing it with the keyboard) shows the full text."""

from gi.repository import Gtk


def tip_if_trimmed(label: Gtk.Label) -> Gtk.Label:
    """Show the label's full text as a tooltip, but only while it is actually cut off."""
    label.set_has_tooltip(True)
    label.connect("query-tooltip", _on_query_tooltip)
    return label


def _on_query_tooltip(
    label: Gtk.Label, _x: int, _y: int, _keyboard: bool, tooltip: Gtk.Tooltip
) -> bool:
    if label.get_layout().is_ellipsized():
        tooltip.set_text(label.get_text())
        return True
    return False
