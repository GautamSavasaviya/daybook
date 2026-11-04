"""The app's button rules, in one place (D-115):

- A button with text is a normal, outlined button. `suggested` marks the one main action of
  a screen, `destructive` an action that deletes.
- `flat` is for buttons with only an icon (close, delete, previous/next); flat text looks
  like plain bold text, not like something to press.
- `pill` (and `suggested`) is for the action on an empty page.
- Actions side by side sit in one `button_bar`, so their spacing, and the gap above them,
  are the same everywhere.
"""

from collections.abc import Callable
from enum import StrEnum

from gi.repository import Adw, Gtk


class Kind(StrEnum):
    NORMAL = ""
    SUGGESTED = "suggested-action"
    DESTRUCTIVE = "destructive-action"


def text_button(
    label: str,
    on_clicked: Callable[[Gtk.Button], None] | None = None,
    *,
    kind: Kind = Kind.NORMAL,
    tooltip: str | None = None,
) -> Gtk.Button:
    """A button with a label; underscores in `label` set a mnemonic."""
    button = Gtk.Button(label=label, use_underline=True, valign=Gtk.Align.CENTER)
    if kind != Kind.NORMAL:
        button.add_css_class(kind.value)
    if tooltip:
        button.set_tooltip_text(tooltip)
    if on_clicked is not None:
        button.connect("clicked", on_clicked)
    return button


def empty_page_button(label: str, on_clicked: Callable[[Gtk.Button], None]) -> Gtk.Button:
    """The one action of an empty page (Adw.StatusPage child)."""
    button = text_button(label, on_clicked, kind=Kind.SUGGESTED)
    button.add_css_class("pill")
    button.set_halign(Gtk.Align.CENTER)
    return button


def button_bar(*buttons: Gtk.Widget, align: Gtk.Align = Gtk.Align.END) -> Adw.WrapBox:
    """Buttons in a row that wraps on narrow windows."""
    bar = Adw.WrapBox(child_spacing=6, line_spacing=6, halign=align, margin_top=12)
    for button in buttons:
        bar.append(button)
    return bar
