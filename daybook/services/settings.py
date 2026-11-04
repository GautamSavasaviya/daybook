"""Read and change user settings; publishes `Changed(SETTINGS)` after each save."""

from daybook.core.db import Connection
from daybook.core.repo import settings as settings_repo
from daybook.core.settings import Settings
from daybook.services.events import Changed, Entity, EventBus


class SettingsService:
    def __init__(self, conn: Connection, bus: EventBus) -> None:
        self._conn = conn
        self._bus = bus
        self._current = settings_repo.load(conn)

    @property
    def current(self) -> Settings:
        return self._current

    def reload(self) -> None:
        """Read the settings again, after the data was replaced (restore or import)."""
        self._current = settings_repo.load(self._conn)
        self._bus.publish(Changed(Entity.SETTINGS))

    def save(self, new: Settings) -> Settings:
        """Store `new` (typically ``dataclasses.replace(service.current, ...)``).

        Raises ValidationError, saving nothing, if it is invalid.
        """
        settings_repo.save(self._conn, new)
        self._current = new
        self._bus.publish(Changed(Entity.SETTINGS))
        return new
