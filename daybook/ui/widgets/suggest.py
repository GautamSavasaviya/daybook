"""Autocomplete for an Adw.EntryRow (GtkEntryCompletion is deprecated, PLAN §1).

Suggestions appear in a popover under the row while typing. The keyboard stays in the
entry: Down/Up pick a suggestion, Enter takes it, Escape hides the list.
"""

from collections.abc import Callable

from gi.repository import Adw, Gdk, GLib, Gtk

from daybook.services.safe import safe_callback


class Suggestions:
    def __init__(
        self,
        row: Adw.EntryRow,
        provider: Callable[[str], list[str]],
        on_accept: Callable[[str], None] | None = None,
        *,
        insert: Callable[[str], None] | None = None,
    ) -> None:
        self._row = row
        self._provider = provider
        self._on_accept = on_accept
        # By default a suggestion replaces the whole text; `insert` puts it in itself
        # (quick add completes only the word being typed).
        self._insert = insert
        self._accepting = False

        # Nothing in the popover takes focus, so typing continues in the entry.
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, can_focus=False)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_row_activated)
        self._popover = Gtk.Popover(
            child=self._list,
            autohide=False,
            has_arrow=False,
            can_focus=False,
            position=Gtk.PositionType.BOTTOM,
            halign=Gtk.Align.START,
        )
        self._popover.set_parent(row)
        row.connect("changed", self._on_changed)
        row.connect("destroy", self._on_destroy)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        row.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", self._on_focus_leave)
        row.add_controller(focus)

    def _values(self) -> list[str]:
        return self._provider(self._row.get_text())

    def _show(self, values: list[str]) -> None:
        self._list.remove_all()
        for value in values:
            label = Gtk.Label(label=value, xalign=0, use_markup=False)
            self._list.append(Gtk.ListBoxRow(child=label, name=value, focusable=False))
        if values and self._row_focused():
            self._popover.set_size_request(max(200, self._row.get_width() - 24), -1)
            self._popover.popup()
        else:
            self._popover.popdown()

    def _row_focused(self) -> bool:
        # The focus is on the EntryRow's inner text widget, not on the row itself.
        root = self._row.get_root()
        focus = root.get_focus() if isinstance(root, Gtk.Window) else None
        return focus is not None and (focus is self._row or focus.is_ancestor(self._row))

    def _accept(self, value: str) -> None:
        self._accepting = True
        try:
            if self._insert is not None:
                self._insert(value)
            else:
                self._row.set_text(value)
                self._row.set_position(-1)
        finally:
            self._accepting = False
        self._popover.popdown()
        if self._on_accept is not None:
            self._on_accept(value)

    @safe_callback
    def _on_changed(self, _row: Adw.EntryRow) -> None:
        if not self._accepting:
            self._show(self._values())

    @safe_callback
    def _on_key(
        self, _controller: Gtk.EventControllerKey, keyval: int, _code: int, _state: Gdk.ModifierType
    ) -> bool:
        if not self._popover.get_visible():
            return False
        selected = self._list.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            index = -1 if selected is None else selected.get_index()
            index += 1 if keyval == Gdk.KEY_Down else -1
            row = self._list.get_row_at_index(max(0, index))
            if row is not None:
                self._list.select_row(row)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and selected is not None:
            self._accept(selected.get_name())
            return True
        if keyval == Gdk.KEY_Escape:
            self._popover.popdown()
            return True
        return False

    @safe_callback
    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._accept(row.get_name())
        self._row.grab_focus()

    @safe_callback
    def _on_focus_leave(self, _controller: Gtk.EventControllerFocus) -> None:
        # Wait a moment: a click on a suggestion must land before the list goes away.
        GLib.timeout_add(200, self._hide_unless_focused)

    @safe_callback
    def _hide_unless_focused(self) -> bool:
        if not self._row_focused():
            self._popover.popdown()
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_destroy(self, _row: Adw.EntryRow) -> None:
        self._popover.unparent()
