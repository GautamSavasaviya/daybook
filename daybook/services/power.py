"""Suspend and shutdown from logind (system bus): reminders are re-checked after resume and
the timer heartbeat is written before sleep or shutdown. Without logind this does nothing."""

import logging
from collections.abc import Callable

from gi.repository import Gio, GLib

from daybook.services.safe import report_error

log = logging.getLogger(__name__)

LOGIND = "org.freedesktop.login1"
MANAGER = "org.freedesktop.login1.Manager"
PATH = "/org/freedesktop/login1"


class PowerMonitor:
    def __init__(
        self,
        *,
        on_sleep: Callable[[], None],
        on_resume: Callable[[], None],
        on_shutdown: Callable[[], None],
    ) -> None:
        self._on_sleep = on_sleep
        self._on_resume = on_resume
        self._on_shutdown = on_shutdown
        self._bus: Gio.DBusConnection | None = None
        self._subscriptions: list[int] = []
        self._stopped = False

    def start(self) -> None:
        Gio.bus_get(Gio.BusType.SYSTEM, None, self._on_bus)

    def stop(self) -> None:
        self._stopped = True
        if self._bus is not None:
            for subscription in self._subscriptions:
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions.clear()
        self._bus = None

    def _on_bus(self, _source: object, result: Gio.AsyncResult) -> None:
        try:
            self._bus = Gio.bus_get_finish(result)
        except GLib.Error as exc:
            log.info("no system bus, so suspend is noticed only by the clock check: %s", exc)
            return
        if self._stopped:
            self._bus = None
            return
        for signal in ("PrepareForSleep", "PrepareForShutdown"):
            self._subscriptions.append(
                self._bus.signal_subscribe(
                    LOGIND, MANAGER, signal, PATH, None, Gio.DBusSignalFlags.NONE, self._on_signal
                )
            )

    def _on_signal(
        self,
        _connection: Gio.DBusConnection,
        _sender: str | None,
        _path: str,
        _interface: str,
        signal: str,
        parameters: GLib.Variant,
    ) -> None:
        try:
            self.handle(signal, bool(parameters.unpack()[0]))
        except Exception as exc:
            report_error(exc)

    def handle(self, signal: str, starting: bool) -> None:
        if signal == "PrepareForSleep":
            (self._on_sleep if starting else self._on_resume)()
        elif signal == "PrepareForShutdown" and starting:
            self._on_shutdown()
