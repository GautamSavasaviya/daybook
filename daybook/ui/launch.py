"""Open links (meeting links, tickets) in the user's default app."""

from gettext import gettext as _

from gi.repository import Gio, GLib, Gtk

from daybook.services.events import EventBus, Toast
from daybook.services.safe import safe_callback


def open_uri(widget: Gtk.Widget, uri: str, bus: EventBus) -> None:
    """Launch `uri`; a failure (no handler, bad link) shows a toast."""
    root = widget.get_root()
    window = root if isinstance(root, Gtk.Window) else None

    @safe_callback
    def finished(launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                bus.publish(Toast(_("Couldn't open the link")))

    Gtk.UriLauncher.new(uri).launch(window, None, finished)
