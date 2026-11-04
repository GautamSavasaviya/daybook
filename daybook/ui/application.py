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
from daybook.services.events import ShowView, Toast
from daybook.services.notifier import ACTION
from daybook.services.safe import error_message, safe_callback, set_error_reporter
from daybook.services.status import status_line
from daybook.ui.dialogs.preferences import PreferencesDialog
from daybook.ui.dialogs.timer_recovery import ask_recovery
from daybook.ui.sections import sections
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
        self._background = False
        for name, description in (
            ("debug", _("Also write log messages to the terminal")),
            ("quick-add", _("Open Quick Add; any text after it is filled in")),
            ("meeting-now", _("Start an unplanned meeting")),
            ("background", _("Start without a window, for reminders (used at login)")),
            ("status", _("Print the timed task and its time left, then exit")),
        ):
            self.add_main_option(
                name, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, description, None
            )
        self.add_main_option(
            GLib.OPTION_REMAINING,
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.STRING_ARRAY,
            _("Text for --quick-add"),
            _("[TEXT…]"),
        )

    # Lifecycle ----------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(self._pkgdatadir / "icons"))
            css = Gtk.CssProvider()
            css.load_from_path(str(Path(__file__).with_name("style.css")))
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        set_error_reporter(self.show_error)
        self._add_actions()
        try:
            self.context = AppContext.open(self._paths, self._clock, call_soon)
        except Exception as exc:
            log.exception("could not open the database at %s", self._paths.db_file)
            self.startup_error = exc
        else:
            self.context.day.start()
            self.context.timer.start_heartbeat()
            self.context.notifier.attach(self)
            self.context.bus.subscribe(ShowView, self._on_show_view)
            self.context.reminders.start()
            self.context.power.start()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """Runs in the first instance, also for a later `daybook …` forwarded to it."""
        options = command_line.get_options_dict().end().unpack()
        log.info(
            "command line %s (%s)",
            sorted(k for k in options if k) or "-",
            "forwarded" if command_line.get_is_remote() else "first instance",
        )
        if options.get("status"):
            command_line.print_literal(self._status() + "\n")
            return 0
        if options.get("background"):
            if not command_line.get_is_remote() and not self._background:
                # No window: the app stays for reminders until Quit (D-107).
                self._background = True
                self.hold()
            return 0
        if self.context is None:
            self.activate()  # the error window explains what went wrong
            return 0
        text = " ".join(options.get(GLib.OPTION_REMAINING, []))
        if options.get("quick-add"):
            self._present_main().quick_add(text)
        elif options.get("meeting-now"):
            self._present_main().meeting_now()
        else:
            self.activate()
        return 0

    def _status(self) -> str:
        if self.context is None:
            return _("Daybook could not open its data")
        return status_line(self.context.conn, self._clock.now(), self._clock.local_tz())

    def do_activate(self) -> None:
        self._present_main()

    def _present_main(self) -> MainWindow:
        window = self._main_window()
        window.present()
        if (
            self.context is not None
            and self.context.timer.pending_recovery() is not None
            and window.get_visible_dialog() is None
        ):
            ask_recovery(window, self.context)
        return window

    def do_shutdown(self) -> None:
        if self.context is not None:
            for window in self.get_windows():
                if isinstance(window, MainWindow):
                    try:
                        window.flush_pending()
                    except Exception:
                        log.exception("could not save pending edits during shutdown")
            self.context.close()
            self.context = None
        set_error_reporter(None)
        Adw.Application.do_shutdown(self)

    # Actions ------------------------------------------------------------------------

    def _add_actions(self) -> None:
        self._add_action("quit", self._on_quit, ["<Control>q"])
        self._add_action("about", self._on_about)
        self._add_action("preferences", self._on_preferences, ["<Control>comma"])
        # Notification buttons and clicks: app.reminder((key, action)), also over D-Bus
        # when Daybook wasn't running (D-029, D-038).
        reminder = Gio.SimpleAction.new(ACTION, GLib.VariantType("(ss)"))
        reminder.connect("activate", self._on_reminder_action)
        self.add_action(reminder)
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_accels_for_action("win.new-task", ["<Control>n"])
        self.set_accels_for_action("win.search", ["<Control>f"])
        self.set_accels_for_action("win.meeting-now", ["<Control><Shift>m"])
        self.set_accels_for_action("win.done-selected", ["<Control>Return", "<Control>KP_Enter"])
        self.set_accels_for_action("win.quick-add", ["<Control>k"])
        self.set_accels_for_action("win.shortcuts", ["<Control>question"])
        for index, section in enumerate(sections(), start=1):
            self.set_accels_for_action(f"win.section::{section.key}", [f"<Control>{index}"])

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

    def _main_window(self) -> MainWindow:
        window = next((w for w in self.get_windows() if isinstance(w, MainWindow)), None)
        if window is None:
            window = MainWindow(self, self._paths, self.context, self.startup_error)
        return window

    def _on_preferences(self) -> None:
        if self.context is None:
            return
        window = self._main_window()
        window.present()
        PreferencesDialog(self.context, self.get_application_id() or APP_ID).present(window)

    @safe_callback
    def _on_reminder_action(self, _action: Gio.SimpleAction, param: GLib.Variant | None) -> None:
        if self.context is None or param is None:
            return
        key, action = param.unpack()
        self.context.reminders.handle(key, action)

    def _on_show_view(self, event: ShowView) -> None:
        window = self._main_window()
        window.present()
        window.show_view(event.view)

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
