"""Idle and screen-lock detection (D-042, SPEC §7), degrading gracefully.

Every few seconds it asks GNOME's idle monitor how long the user has been away from the
keyboard and listens for the screen locking. If neither is available it simply never
reports anything (Preferences says so). The decisions are `core/idle.py`'s.
"""

import logging
from collections.abc import Callable
from datetime import timedelta

from gi.repository import Gio, GLib

from daybook.core.clock import Clock
from daybook.core.idle import IdleTracker
from daybook.services.events import AwayDetected, EventBus
from daybook.services.safe import report_error
from daybook.services.settings import SettingsService
from daybook.services.timer import TimerService

log = logging.getLogger(__name__)

POLL_SECONDS = 10
MONITOR = ("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core")
SAVER = ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver")


class IdleService:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        *,
        settings: SettingsService,
        timer: TimerService,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._settings = settings
        self._timer = timer
        self._tracker = IdleTracker(self._threshold())
        self._locked = False
        self._idle: timedelta | None = None
        self.monitor_available = False
        self.saver_available = False
        self._conn: Gio.DBusConnection | None = None
        self._subscription: int | None = None
        self._source_id: int | None = None

    def _threshold(self) -> timedelta:
        return timedelta(minutes=self._settings.current.idle_threshold_min)

    # What the decision needs --------------------------------------------------------------

    def observe(self, idle: timedelta | None, locked: bool) -> None:
        """One look at the desktop; publishes `AwayDetected` when the user is back."""
        self._tracker.threshold = self._threshold()
        state = self._timer.state()
        running = state is not None and state.running
        away = self._tracker.observe(self._clock.now(), idle, locked, running)
        if away is not None and state is not None:
            log.info("back after %d min away", away.minutes)
            self._bus.publish(AwayDetected(away.start, away.end, state.task_id))

    # D-Bus -----------------------------------------------------------------------------

    def start(self) -> None:
        Gio.bus_get(Gio.BusType.SESSION, None, self._on_bus)

    def stop(self) -> None:
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None
        if self._conn is not None and self._subscription is not None:
            self._conn.signal_unsubscribe(self._subscription)
        self._subscription = None

    def _on_bus(self, _source: object, result: Gio.AsyncResult) -> None:
        try:
            self._conn = Gio.bus_get_finish(result)
        except GLib.Error as exc:
            log.info("no session bus, so idle detection is off: %s", exc)
            return
        self._subscription = self._conn.signal_subscribe(
            SAVER[0],
            SAVER[0],
            "ActiveChanged",
            SAVER[1],
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_lock_signal,
        )
        self._call(SAVER, "GetActive", None, self._on_saver_state)
        self._call(MONITOR, "GetIdletime", None, self._on_idle_time)
        self._source_id = GLib.timeout_add_seconds(POLL_SECONDS, self._poll)

    def _call(
        self,
        target: tuple[str, str],
        method: str,
        args: GLib.Variant | None,
        done: Callable[[Gio.DBusConnection, Gio.AsyncResult], None],
    ) -> None:
        assert self._conn is not None
        name, path = target
        interface = name
        self._conn.call(
            name,
            path,
            interface,
            method,
            args,
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            done,
        )

    def _reply(self, result: Gio.AsyncResult) -> GLib.Variant | None:
        assert self._conn is not None
        try:
            return self._conn.call_finish(result)
        except GLib.Error:
            return None

    def _on_saver_state(self, _source: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        reply = self._reply(result)
        self.saver_available = reply is not None
        if reply is not None:
            self._locked = bool(reply.unpack()[0])

    def _on_idle_time(self, _source: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        reply = self._reply(result)
        self.monitor_available = reply is not None
        self._idle = timedelta(milliseconds=reply.unpack()[0]) if reply is not None else None
        try:
            self.observe(self._idle, self._locked)
        except Exception as exc:
            report_error(exc)

    def _on_lock_signal(
        self,
        _connection: Gio.DBusConnection,
        _sender: str | None,
        _path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
    ) -> None:
        self._locked = bool(parameters.unpack()[0])
        try:
            self.observe(self._idle, self._locked)
        except Exception as exc:
            report_error(exc)

    def _poll(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the polling for good.
        if self._conn is not None:
            self._call(MONITOR, "GetIdletime", None, self._on_idle_time)
        return GLib.SOURCE_CONTINUE
