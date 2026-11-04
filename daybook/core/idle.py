"""Away detection (D-042). Pure: feed it what the desktop reports, it says when the user
comes back from an absence long enough to ask about.

Idle past the threshold, or a locked screen, while the timer runs starts an absence at the
moment it really began (`now - idle time`, or the lock). When the user returns, the absence
is reported once. Discarding it is a split of the running segment (`core/timer.py`), so it
can never create an overlap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Away:
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def minutes(self) -> int:
        return int(self.duration.total_seconds() // 60)


class IdleTracker:
    def __init__(self, threshold: timedelta) -> None:
        self.threshold = threshold
        self._since: datetime | None = None

    @property
    def away_since(self) -> datetime | None:
        return self._since

    def observe(
        self, now: datetime, idle: timedelta | None, locked: bool, timer_running: bool
    ) -> Away | None:
        """One look at the desktop. `idle` is None when the idle monitor isn't available."""
        if not timer_running:
            self._since = None  # nothing is being timed, so there is nothing to discard
            return None
        quiet = idle is not None and idle >= self.threshold
        if self._since is None:
            if locked or quiet:
                began = now - (idle if idle is not None else timedelta())
                self._since = min(began, now)
            return None
        if locked or quiet:
            return None  # still away
        away = Away(self._since, now)
        self._since = None
        # A short lock (a coffee refill) isn't worth a question.
        return away if away.duration >= self.threshold else None
