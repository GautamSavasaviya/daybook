"""DaybookApplication: single instance, actions, command line and lifecycle."""

import logging
from collections.abc import Callable
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from daybook.const import APP_ID, APP_NAME, VERSION
from daybook.core.clock import Clock
from daybook.paths import Paths
from daybook.services.context import AppContext
from daybook.services.events import Toast
from daybook.services.safe import error_message, safe_callback, set_error_reporter
from daybook.ui.window import MainWindow

log = logging.getLogger(__name__)


class DaybookApplication(Adw.Application):
    def __init__(self, *, app_id: str, clock: Clock, paths: Paths, pkgdatadir: Path) -> None:
        super().__init__(application_id=app_id, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self._clock = clock
        self._paths = paths
        self._pkgdatadir = pkgdatadir
        self.context: AppContext | None = None
        self.startup_error: BaseException | None = None
        self.add_main_option(
            "debug",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            _("Also write log messages to the terminal"),
            None,
        )

    # Lifecycle ----------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(self._pkgdatadir / "icons"))
        set_error_reporter(self.show_error)
        self._add_actions()
        try:
            self.context = AppContext.open(self._paths, self._clock, call_soon)
        except Exception as exc:
            log.exception("could not open the database at %s", self._paths.db_file)
            self.startup_error = exc

    def do_command_line(self, _command_line: Gio.ApplicationCommandLine) -> int:
        self.activate()
        return 0

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            window = MainWindow(self, self._paths, self.context, self.startup_error)
        window.present()

    def do_shutdown(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        set_error_reporter(None)
        Adw.Application.do_shutdown(self)

    # Actions ------------------------------------------------------------------------

    def _add_actions(self) -> None:
        self._add_action("quit", self._on_quit, ["<Control>q"])
        self._add_action("about", self._on_about)
        self.set_accels_for_action("window.close", ["<Control>w"])

    def _add_action(
        self, name: str, handler: Callable[[], None], accels: list[str] | None = None
    ) -> None:
        action = Gio.SimpleAction.new(name, None)

        def on_activate(_action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
            handler()

        action.connect("activate", safe_callback(on_activate))
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def _on_quit(self) -> None:
        self.quit()

    def _on_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            developer_name=_("Daybook contributors"),
            comments=_("Plan your workday, track your time and get reminded at the right moment."),
            license_type=Gtk.License.GPL_3_0,
            debug_info=self._debug_info(),
            debug_info_filename="daybook-debug-info.txt",
        )
        about.present(self.get_active_window())

    def _debug_info(self) -> str:
        return "\n".join(
            (
                f"Daybook {VERSION} ({self.get_application_id()})",
                f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}."
                f"{Gtk.get_micro_version()}",
                f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}."
                f"{Adw.get_micro_version()}",
                f"Data: {self._paths.data_dir}",
                f"Log: {self._paths.log_file}",
            )
        )

    # Errors -------------------------------------------------------------------------

    def show_error(self, _exc: BaseException) -> None:
        """Error reporter: a toast in the main window. Safe to call from any thread."""
        if self.context is not None:
            self.context.bus.publish(Toast(error_message()))


def call_soon(callback: Callable[[], None]) -> None:
    """Run `callback` once on the GLib main loop (thread-safe)."""

    def run_once() -> bool:
        callback()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(safe_callback(run_once))
