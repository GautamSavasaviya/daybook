"""Injectable clocks — the only module that reads the real time.

Everything that depends on time takes a `Clock`. Production uses `SystemClock`; the dev
variables ``DAYBOOK_TIME_SCALE`` and ``DAYBOOK_FAKE_NOW`` select a `ScaledClock`; tests use
`ManualClock`. Without those variables set, nothing changes (SPEC, Engineering quality bar).
"""

import contextlib
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daybook.core.timeutil import localize

log = logging.getLogger(__name__)

TIME_SCALE_VAR = "DAYBOOK_TIME_SCALE"
FAKE_NOW_VAR = "DAYBOOK_FAKE_NOW"

ZoneResolver = Callable[[], ZoneInfo]


class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant, timezone-aware, in UTC."""
        ...

    def local_tz(self) -> ZoneInfo:
        """The user's local timezone, as of the last refresh."""
        ...

    def refresh_local_tz(self) -> bool:
        """Re-read the system timezone. Returns True when it changed (D-014)."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never jumps; compared with now() to detect wall-clock jumps."""
        ...

    def real_seconds_until(self, when: datetime) -> float:
        """Real seconds to wait until this clock reaches `when` (0 if already past)."""
        ...


def resolve_local_zone(
    environ: Mapping[str, str],
    localtime: Path = Path("/etc/localtime"),
    timezone_file: Path = Path("/etc/timezone"),
) -> ZoneInfo:
    """Find the IANA zone in effect: ``$TZ``, then the /etc/localtime link, then /etc/timezone.

    Falls back to the raw /etc/localtime data, and finally to UTC, so it never raises.
    """
    keys: list[str] = []
    tz_env = environ.get("TZ", "").lstrip(":")
    if tz_env:
        keys.append(tz_env)
    try:
        parts = localtime.resolve().parts
        if "zoneinfo" in parts:
            index = len(parts) - 1 - parts[::-1].index("zoneinfo")
            keys.append("/".join(parts[index + 1 :]))
    except OSError:
        pass
    with contextlib.suppress(OSError):
        keys.append(timezone_file.read_text(encoding="utf-8").strip())
    for key in keys:
        try:
            return ZoneInfo(key)
        except ZoneInfoNotFoundError, ValueError:
            continue
    try:
        with localtime.open("rb") as data:
            return ZoneInfo.from_file(data, key="localtime")
    except OSError, ValueError:
        log.warning("could not determine the local timezone; using UTC")
        return ZoneInfo("UTC")


def _system_zone() -> ZoneInfo:
    return resolve_local_zone(os.environ)


class _ZoneTracker:
    def __init__(self, resolver: ZoneResolver) -> None:
        self._resolve = resolver
        self._tz = resolver()

    def local_tz(self) -> ZoneInfo:
        return self._tz

    def refresh_local_tz(self) -> bool:
        new = self._resolve()
        if str(new) == str(self._tz):
            return False
        log.info("local timezone changed: %s -> %s", self._tz, new)
        self._tz = new
        time.tzset()
        return True


class SystemClock(_ZoneTracker):
    def __init__(self, zone_resolver: ZoneResolver = _system_zone) -> None:
        super().__init__(zone_resolver)

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def real_seconds_until(self, when: datetime) -> float:
        return max(0.0, (when - self.now()).total_seconds())


class ScaledClock(_ZoneTracker):
    """Starts at `start` and runs `scale` times faster than real time (dev testing only)."""

    def __init__(
        self,
        scale: float,
        start: datetime,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        zone_resolver: ZoneResolver = _system_zone,
    ) -> None:
        if not (math.isfinite(scale) and scale > 0):
            raise ValueError(f"time scale must be a positive number, got {scale!r}")
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        super().__init__(zone_resolver)
        self.scale = scale
        self._start = start.astimezone(UTC)
        self._real_mono = monotonic
        self._t0 = monotonic()

    def _elapsed(self) -> float:
        return (self._real_mono() - self._t0) * self.scale

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed())

    def monotonic(self) -> float:
        return self._t0 + self._elapsed()

    def real_seconds_until(self, when: datetime) -> float:
        return max(0.0, (when - self.now()).total_seconds() / self.scale)


class ManualClock:
    """A clock that moves only when told to. For tests."""

    def __init__(self, start: datetime, tz: ZoneInfo | None = None) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._mono = 1000.0
        self._tz = tz or ZoneInfo("UTC")
        self._pending_tz: ZoneInfo | None = None

    def now(self) -> datetime:
        return self._now

    def local_tz(self) -> ZoneInfo:
        return self._tz

    def refresh_local_tz(self) -> bool:
        if self._pending_tz is None or str(self._pending_tz) == str(self._tz):
            self._pending_tz = None
            return False
        self._tz, self._pending_tz = self._pending_tz, None
        return True

    def monotonic(self) -> float:
        return self._mono

    def real_seconds_until(self, when: datetime) -> float:
        return max(0.0, (when - self._now).total_seconds())

    def advance(self, delta: timedelta) -> None:
        """Let time pass normally: wall clock and monotonic clock move together."""
        if delta < timedelta(0):
            raise ValueError("time cannot pass backwards; use jump() for a clock change")
        self._now += delta
        self._mono += delta.total_seconds()

    def jump(self, to: datetime) -> None:
        """Change the wall clock without time passing (NTP correction, suspend, manual set)."""
        self._now = to.astimezone(UTC)

    def set_system_tz(self, tz: ZoneInfo) -> None:
        """Simulate the user changing the system timezone; takes effect on refresh_local_tz()."""
        self._pending_tz = tz


def clock_from_env(environ: Mapping[str, str]) -> Clock:
    """`SystemClock`, unless the dev variables ask for scaled or shifted time.

    Invalid values are logged and ignored rather than stopping the app.
    """
    scale_raw = environ.get(TIME_SCALE_VAR, "").strip()
    fake_raw = environ.get(FAKE_NOW_VAR, "").strip()
    if not scale_raw and not fake_raw:
        return SystemClock()

    scale = 1.0
    if scale_raw:
        try:
            scale = float(scale_raw)
            if not (math.isfinite(scale) and scale > 0):
                raise ValueError(scale_raw)
        except ValueError:
            log.warning("ignoring %s=%r: expected a positive number", TIME_SCALE_VAR, scale_raw)
            scale = 1.0

    start: datetime | None = None
    if fake_raw:
        try:
            parsed = datetime.fromisoformat(fake_raw)
            start = parsed if parsed.tzinfo else localize(parsed, _system_zone())
        except ValueError:
            log.warning("ignoring %s=%r: expected an ISO 8601 date-time", FAKE_NOW_VAR, fake_raw)

    if scale == 1.0 and start is None:
        return SystemClock()
    log.warning(
        "dev clock active: time scale %s, starting at %s", scale, (start or "the real time")
    )
    return ScaledClock(scale, start or datetime.now(UTC))
