"""Copy to the clipboard and save a file, with a toast either way."""

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, Gtk

from daybook.services.context import AppContext
from daybook.services.events import Toast
from daybook.services.safe import safe_callback


def copy_text(widget: Gtk.Widget, context: AppContext, text: str, what: str) -> None:
    """Put `text` on the clipboard and say so: "Standup copied"."""
    clipboard: Gdk.Clipboard = widget.get_clipboard()
    clipboard.set(text)
    context.bus.publish(Toast(_("{what} copied").format(what=what)))


def save_text(
    widget: Gtk.Widget,
    context: AppContext,
    text: str,
    *,
    filename: str,
    title: str,
    on_saved: Callable[[], None] | None = None,
) -> None:
    """Ask where to save (Gtk.FileDialog) and write `text` there."""
    dialog = Gtk.FileDialog(title=title, initial_name=filename)
    csv_filter = Gtk.FileFilter(name=_("CSV files"))
    csv_filter.add_suffix("csv")
    dialog.set_default_filter(csv_filter)
    root = widget.get_root()
    parent = root if isinstance(root, Gtk.Window) else None

    def done(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file = source.save_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                context.bus.publish(Toast(_("Couldn't save the file")))
            return
        try:
            file.replace_contents(
                text.encode(), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None
            )
        except GLib.Error:
            context.bus.publish(Toast(_("Couldn't save the file")))
            return
        context.bus.publish(Toast(_("Saved {name}").format(name=file.get_basename())))
        if on_saved is not None:
            on_saved()

    dialog.save(parent, None, safe_callback(done))
