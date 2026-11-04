"""Time formatting for the UI: follows GNOME's 12/24-hour clock setting (D-034)."""

from datetime import datetime, tzinfo

from gi.repository import Gio

from daybook.core.timeutil import fmt_clock

_SCHEMA = "org.gnome.desktop.interface"


class ClockFormat:
    def __init__(self) -> None:
        self._settings: Gio.Settings | None = None
        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup(_SCHEMA, True) is not None:
            self._settings = Gio.Settings.new(_SCHEMA)

    @property
    def use_24h(self) -> bool:
        if self._settings is None:
            return True
        return bool(self._settings.get_string("clock-format") != "12h")

    def time(self, instant: datetime, tz: tzinfo) -> str:
        return fmt_clock(instant, tz, use_24h=self.use_24h)

    def span(self, start: datetime, end: datetime, tz: tzinfo) -> str:
        return f"{self.time(start, tz)}–{self.time(end, tz)}"


_shared: ClockFormat | None = None


def clock_format() -> ClockFormat:
    global _shared  # noqa: PLW0603 — one reader of the GNOME setting per process
    if _shared is None:
        _shared = ClockFormat()
    return _shared
