"""Entry point, called by the installed ``daybook`` launcher."""

import gettext
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import gi

from daybook.const import APP_ID, GETTEXT_DOMAIN, VERSION
from daybook.core.clock import Clock, clock_from_env
from daybook.core.db import connect
from daybook.logsetup import install_excepthooks, setup_logging
from daybook.paths import application_id, dev_override_problem, resolve_paths
from daybook.services.status import status_line

log = logging.getLogger(__name__)


def _quit_on_signals(quit_app: Callable[[], None]) -> None:
    """Ctrl+C and SIGTERM (logout, shutdown) quit through the normal shutdown path, so the
    timer state is saved and the database closed (SPEC: shut down cleanly)."""

    def handler(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s; quitting", signal.Signals(signum).name)
        quit_app()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _instance_running(app_id: str) -> bool:
    """Whether `app_id` is owned on the session bus, checked without registering ourselves."""
    from gi.repository import Gio, GLib  # noqa: PLC0415 — needs gi versions fixed first

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (app_id,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
    except GLib.Error:
        log.warning("could not ask the session bus whether Daybook is running", exc_info=True)
        return False
    return bool(reply.unpack()[0])


def offline_status(db_file: Path, clock: Clock) -> tuple[str, int]:
    """`--status` when Daybook isn't running: a read-only look at the saved timer."""
    if not db_file.exists():
        return gettext.gettext("No timer running"), 0
    try:
        conn = connect(db_file, read_only=True)
    except sqlite3.Error:
        log.exception("could not open %s read-only", db_file)
        return gettext.gettext("Daybook could not read its data"), 1
    try:
        return status_line(conn, clock.now(), clock.local_tz()), 0
    except sqlite3.Error:
        log.exception("could not read the timer from %s", db_file)
        return gettext.gettext("Daybook could not read its data"), 1
    finally:
        conn.close()


def main(argv: list[str], *, pkgdatadir: str) -> int:
    gettext.textdomain(GETTEXT_DOMAIN)
    problem = dev_override_problem(os.environ)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    paths = resolve_paths(os.environ)
    setup_logging(paths.log_file, debug="--debug" in argv[1:])
    clock = clock_from_env(os.environ)
    app_id = application_id(os.environ)

    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    # Imported only now: the GTK/Adw versions above must be fixed before gi.repository loads.
    from daybook.ui.application import DaybookApplication  # noqa: PLC0415

    if app_id != APP_ID and _instance_running(app_id):
        # Test runs share one Devel ID (D-065). A second one would be forwarded to the first,
        # which may use a different DAYBOOK_DATA_DIR, so refuse with a clear message instead.
        print(
            "A Daybook test instance (DAYBOOK_* variables) is already running; quit it first.",
            file=sys.stderr,
        )
        return 1

    if "--status" in argv[1:] and not _instance_running(app_id):
        # Nothing to forward to: read the saved timer without starting Daybook (D-031).
        line, code = offline_status(paths.db_file, clock)
        print(line)
        return code

    app = DaybookApplication(app_id=app_id, clock=clock, paths=paths, pkgdatadir=Path(pkgdatadir))
    install_excepthooks(app.show_error)
    _quit_on_signals(app.quit)
    log.info("Daybook %s starting as %s (data: %s)", VERSION, app_id, paths.data_dir)
    return app.run(argv)
