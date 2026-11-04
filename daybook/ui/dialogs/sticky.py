"""A dialog that stays open when you click outside it (D-119).

Adw.Dialog closes on a click outside, which throws away whatever was typed in a form.
A `StickyDialog` ignores outside clicks and closes only on purpose: its Cancel or close
button, Escape, or a successful save (all of which call `close()`).
"""

from gi.repository import Adw, Gdk, Gtk

from daybook.services.safe import report_error


class StickyDialog(Adw.Dialog):
    def __init__(self, *, title: str, content_width: int = -1, content_height: int = -1) -> None:
        super().__init__(title=title, content_width=content_width, content_height=content_height)
        # An outside click (and the library's own Escape handling) now only emits
        # `close-attempt`, which nothing answers.
        self.set_can_close(False)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    def close(self) -> bool:
        """Close on purpose. (Adw.Dialog.close would be refused by `can-close`.)"""
        self.force_close()
        return True

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        # Not @safe_callback: the handler must return a bool.
        if keyval != Gdk.KEY_Escape:
            return False
        try:
            self.close()
        except Exception as exc:
            report_error(exc)
        return True
