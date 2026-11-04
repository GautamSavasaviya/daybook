"""Copy to the clipboard and save a file, with a toast either way."""

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, Gtk

from daybook.services.context import AppContext
from daybook.services.events import Toast
from daybook.services.safe import safe_callback


def _filter(kind: str) -> Gtk.FileFilter:
    names = {"csv": _("CSV files"), "json": _("JSON files"), "db": _("Daybook backups")}
    result = Gtk.FileFilter(name=names[kind])
    result.add_suffix(kind)
    return result


MAX_IMPORT_BYTES = 64 * 1024 * 1024


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
    kind: str = "csv",
    on_saved: Callable[[], None] | None = None,
) -> None:
    """Ask where to save (Gtk.FileDialog) and write `text` there."""
    dialog = Gtk.FileDialog(title=title, initial_name=filename)
    dialog.set_default_filter(_filter(kind))
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


def open_text(
    widget: Gtk.Widget,
    context: AppContext,
    *,
    title: str,
    kind: str,
    on_text: Callable[[str], None],
) -> None:
    """Ask for a file (Gtk.FileDialog) and hand its text to `on_text`."""
    dialog = Gtk.FileDialog(title=title)
    dialog.set_default_filter(_filter(kind))
    root = widget.get_root()
    parent = root if isinstance(root, Gtk.Window) else None

    def done(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file = source.open_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                context.bus.publish(Toast(_("Couldn't open the file")))
            return
        try:
            size = file.query_info("standard::size", Gio.FileQueryInfoFlags.NONE, None).get_size()
            if size > MAX_IMPORT_BYTES:
                context.bus.publish(Toast(_("That file is too large to be a Daybook export")))
                return
            _ok, data, _etag = file.load_contents(None)
            text = bytes(data).decode("utf-8")
        except GLib.Error, UnicodeDecodeError:
            context.bus.publish(Toast(_("Couldn't read the file")))
            return
        on_text(text)

    dialog.open(parent, None, safe_callback(done))
