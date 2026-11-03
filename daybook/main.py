"""Entry point, called by the installed ``daybook`` launcher."""

import gettext
import logging
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import gi

from daybook.const import APP_ID, GETTEXT_DOMAIN, VERSION
from daybook.core.clock import clock_from_env
from daybook.logsetup import install_excepthooks, setup_logging
from daybook.paths import active_dev_overrides, resolve_paths

log = logging.getLogger(__name__)


def _quit_on_signals(quit_app: Callable[[], None]) -> None:
    """Ctrl+C and SIGTERM (logout, shutdown) quit through the normal shutdown path, so the
    timer state is saved and the database closed (SPEC: shut down cleanly)."""

    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s; quitting", signal.Signals(signum).name)
        quit_app()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _instance_running() -> bool:
    """Whether the app's D-Bus name is owned, checked without registering ourselves."""
    from gi.repository import Gio, GLib  # noqa: PLC0415 — needs gi versions fixed first

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (APP_ID,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
    except GLib.Error:
        log.warning("could not ask the session bus whether Daybook is running", exc_info=True)
        return False
    return bool(reply.unpack()[0])


def main(argv: list[str], *, pkgdatadir: str) -> int:
    paths = resolve_paths(os.environ)
    setup_logging(paths.log_file, debug="--debug" in argv[1:])
    gettext.textdomain(GETTEXT_DOMAIN)
    clock = clock_from_env(os.environ)

    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    # Imported only now: the GTK/Adw versions above must be fixed before gi.repository loads.
    from daybook.ui.application import DaybookApplication  # noqa: PLC0415

    overrides = active_dev_overrides(os.environ)
    if overrides and _instance_running():
        # D-020: never apply dev overrides to the real data of an already-running instance.
        print(
            f"Another Daybook instance is running; quit it first to use {', '.join(overrides)}.",
            file=sys.stderr,
        )
        return 1

    app = DaybookApplication(clock=clock, paths=paths, pkgdatadir=Path(pkgdatadir))
    install_excepthooks(app.show_error)
    _quit_on_signals(app.quit)
    log.info("Daybook %s starting (data: %s)", VERSION, paths.data_dir)
    return app.run(argv)
