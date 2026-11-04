"""DaybookApplication: single instance, actions, command line and lifecycle."""

import logging
from collections.abc import Callable
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from daybook.const import APP_ID, APP_NAME, VERSION
from daybook.core.clock import Clock
from daybook.core.traymenu import Command
from daybook.paths import Paths
from daybook.services import autostart
from daybook.services.context import AppContext
from daybook.services.events import AwayDetected, Changed, Entity, ShowView, Toast
from daybook.services.notifier import ACTION
from daybook.services.safe import error_message, safe_callback, set_error_reporter
from daybook.services.status import status_line, tray_status
from daybook.services.tray import TrayService
from daybook.ui.dialogs.idle_return import ask_away
from daybook.ui.dialogs.onboarding import OnboardingDialog
from daybook.ui.dialogs.preferences import PreferencesDialog
from daybook.ui.dialogs.quit_confirm import confirm_quit
from daybook.ui.dialogs.timer_recovery import ask_recovery
from daybook.ui.sections import sections
from daybook.ui.window import MainWindow

log = logging.getLogger(__name__)


class DaybookApplication(Adw.Application):
    def __init__(
        self, *, app_id: str, clock: Clock, paths: Paths, pkgdatadir: Path, exec_path: str
    ) -> None:
        super().__init__(application_id=app_id, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self._clock = clock
        self._paths = paths
        self._pkgdatadir = pkgdatadir
        self._exec_path = exec_path
        self._held = False
        self._tray: TrayService | None = None
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
            self.context.idle.start()
            self.context.bus.subscribe(AwayDetected, self._on_away)
            self.context.bus.subscribe(Changed, self._on_changed)
            self.context.backup.run_if_due()
            self._sync_hold()
            self._sync_autostart()
            self._start_tray()

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
                self._sync_hold()
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
        if self._background:
            self._background = False  # a window is open now: the setting decides (D-120)
            self._sync_hold()
        if (
            self.context is not None
            and self.context.timer.pending_recovery() is not None
            and window.get_visible_dialog() is None
        ):
            ask_recovery(window, self.context)
        if (
            self.context is not None
            and window.get_visible_dialog() is None
            and self.context.onboarding.needed()
        ):
            OnboardingDialog(self.context).present(window)
        return window

    # Staying alive, login, tray --------------------------------------------------------

    def _sync_hold(self) -> None:
        """Keep the process running with no window open: while it started hidden, and when
        "Keep running when the window closes" is on (D-120). Quit always really quits."""
        keep = self._background or (
            self.context is not None and self.context.settings.current.run_in_background
        )
        if keep and not self._held:
            self.hold()
            self._held = True
        elif not keep and self._held:
            self.release()
            self._held = False

    def _sync_autostart(self) -> None:
        # A test instance (D-065) must never write the real app's login entry.
        if self.context is None or self.get_application_id() != APP_ID:
            return
        wanted = self.context.settings.current.start_at_login
        try:
            autostart.apply(self._paths.autostart_file, self._exec_path, wanted=wanted)
        except OSError:
            log.exception("could not update the login entry")

    def _start_tray(self) -> None:
        if self.context is None or self.get_application_id() != APP_ID:
            return  # test instances don't add a second icon to the real tray
        context = self.context
        self._tray = TrayService(
            context.bus,
            status=lambda: tray_status(context.conn, self._clock.now(), self._clock.local_tz()),
            on_command=self._on_tray_command,
        )
        Gio.bus_get(Gio.BusType.SESSION, None, self._on_tray_bus)

    def _on_tray_bus(self, _source: object, result: Gio.AsyncResult) -> None:
        try:
            connection = Gio.bus_get_finish(result)
        except GLib.Error:
            log.info("no session bus: no tray icon", exc_info=True)
            return
        if self._tray is not None:
            self._tray.start(connection)

    def _on_tray_command(self, command: Command) -> None:
        context = self.context
        if context is None:
            return
        if command == Command.SHOW:
            self._present_main()
        elif command == Command.QUICK_ADD:
            self._present_main().quick_add()
        elif command == Command.MEETING_NOW:
            self._present_main().meeting_now()
        elif command == Command.TOGGLE_TIMER:
            state = context.timer.state()
            if state is not None:
                context.timer.toggle(state.task_id)
        elif command == Command.QUIT:
            self.request_quit()

    def _on_changed(self, event: Changed) -> None:
        if event.entity == Entity.SETTINGS:
            self._sync_hold()
            self._sync_autostart()

    def _on_away(self, event: AwayDetected) -> None:
        if self.context is not None:
            ask_away(self._present_main(), self.context, event)

    def request_quit(self) -> None:
        """Quit (menu, Ctrl+Q, tray). A running timer is asked about first (SPEC §7)."""
        context = self.context
        state = context.timer.state() if context is not None else None
        if context is None or state is None or not state.running:
            self.quit()
            return
        confirm_quit(self._present_main(), context, self.quit)

    def do_shutdown(self) -> None:
        if self._tray is not None:
            self._tray.stop()
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
        self.request_quit()

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
