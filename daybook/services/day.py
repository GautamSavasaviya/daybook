"""Notices when the local date changes and publishes `DayChanged`."""

import logging
from datetime import date

from gi.repository import GLib

from daybook.core.clock import Clock
from daybook.services.events import DayChanged, EventBus
from daybook.services.safe import report_error

log = logging.getLogger(__name__)

# Polling (rather than one timeout at midnight) also catches suspend, clock changes and
# timezone changes, at the cost of a cheap check twice a minute.
CHECK_INTERVAL_S = 30


class DayWatcher:
    def __init__(self, clock: Clock, bus: EventBus) -> None:
        self._clock = clock
        self._bus = bus
        self._today = self._local_today()
        self._source_id: int | None = None

    def _local_today(self) -> date:
        return self._clock.now().astimezone(self._clock.local_tz()).date()

    @property
    def today(self) -> date:
        return self._today

    def start(self) -> None:
        if self._source_id is None:
            self._source_id = GLib.timeout_add_seconds(CHECK_INTERVAL_S, self._on_tick)

    def stop(self) -> None:
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None

    def check(self) -> bool:
        """Publish `DayChanged` if the local date or the timezone changed since the last
        check (a new zone re-buckets days even when the date stays the same, D-014)."""
        tz_changed = self._clock.refresh_local_tz()
        today = self._local_today()
        if today == self._today and not tz_changed:
            return False
        log.info("local date is now %s (timezone changed: %s)", today, tz_changed)
        self._today = today
        self._bus.publish(DayChanged(today))
        return True

    def _on_tick(self) -> bool:
        # Not @safe_callback: its None result on failure would remove the timeout for good.
        try:
            self.check()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_CONTINUE
