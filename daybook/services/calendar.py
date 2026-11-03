"""Gives every service the same WorkCalendar, rebuilt when settings or time off change."""

from daybook.core.calendar import WorkCalendar
from daybook.core.db import Connection
from daybook.core.repo import time_off as time_off_repo
from daybook.services.events import Changed, Entity, EventBus
from daybook.services.settings import SettingsService


class CalendarService:
    def __init__(self, conn: Connection, settings: SettingsService, bus: EventBus) -> None:
        self._conn = conn
        self._settings = settings
        self._cached: WorkCalendar | None = None
        bus.subscribe(Changed, self._on_changed)

    def calendar(self) -> WorkCalendar:
        if self._cached is None:
            self._cached = WorkCalendar.from_settings(
                self._settings.current, time_off_repo.all_days(self._conn)
            )
        return self._cached

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.SETTINGS, Entity.TIME_OFF):
            self._cached = None
