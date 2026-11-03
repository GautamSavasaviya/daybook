"""Draws the chips decided by `daybook.core.chips` (PLAN §6.2)."""

from gi.repository import Adw, Gtk

from daybook.core.chips import Chip, ChipKind, ChipStyle


def chip_widget(chip: Chip) -> Gtk.Widget:
    classes = ["chip", "caption"]
    if chip.style != ChipStyle.NEUTRAL:
        classes.append(chip.style.value)
    label = Gtk.Label(label=chip.label, css_classes=classes, use_markup=False)
    label.update_property([Gtk.AccessibleProperty.LABEL], [chip.description])
    if chip.kind != ChipKind.REQUESTED_BY or not chip.person:
        return label
    box = Gtk.Box(spacing=4, css_classes=["chip", "person", "caption"])
    box.append(Adw.Avatar(size=16, text=chip.person, show_initials=True))
    label.set_css_classes(["caption"])
    box.append(label)
    box.update_property([Gtk.AccessibleProperty.LABEL], [chip.description])
    return box
