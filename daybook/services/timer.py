"""The focus timer and tracked time (PLAN §5.6): one timer, saved on every change, with a
heartbeat for recovery after a crash, and manual time entries with Undo."""

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from gettext import gettext as _

from gi.repository import GLib

from daybook.core import timer as rules
from daybook.core import timeutil, tracking
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import NotFoundError, OverlapError, ValidationError
from daybook.core.models import Hold, PauseReason, TaskStatus, TimeEntry, TimerState
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_entries
from daybook.core.repo import timer as timer_repo
from daybook.core.timer import Recovery, RecoveryChoice
from daybook.core.tracking import EntryInput, Run
from daybook.services.events import Changed, Entity, EventBus, Toast
from daybook.services.safe import report_error
from daybook.services.text import short

log = logging.getLogger(__name__)

HEARTBEAT = timedelta(seconds=60)


class TimerService:
    def __init__(self, conn: Connection, bus: EventBus, clock: Clock) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._source_id: int | None = None
        # A timer found running at startup waits for the user's answer; until then the
        # heartbeat leaves `last_seen` alone, so "Stop at last activity" stays truthful.
        self._recovery = rules.recovery(timer_repo.get(conn))
        if self._recovery is not None:
            log.info("timer found running since %s: asking what to do", self._recovery.since)

    def _now(self) -> datetime:
        # Stored instants have whole seconds; keeping the same precision in memory means a
        # segment's start is exactly what is read back.
        return self._clock.now().replace(microsecond=0)

    # Reading ----------------------------------------------------------------------------

    def state(self) -> TimerState | None:
        return timer_repo.get(self._conn)

    def running_task(self) -> int | None:
        state = self.state()
        return state.task_id if state is not None and state.running else None

    def tracked_today_s(self, task_id: int) -> int:
        today = self._clock.now().astimezone(self._clock.local_tz()).date()
        tz = self._clock.local_tz()
        return time_entries.tracked(self._conn, task_id, today, tz, self._clock.now()).today_s

    def hold_to_confirm(self, task_id: int) -> Hold | None:
        """The hold the user should confirm leaving before the timer starts (D-046)."""
        task = tasks_repo.get(self._conn, task_id)
        return task.hold if task.status == TaskStatus.ON_HOLD else None

    # Controlling ------------------------------------------------------------------------

    def start(self, task_id: int) -> None:
        """Start timing `task_id`, stopping any other task's timer and saving its time.
        A To do or On hold task becomes In progress; ask first for on-hold ones."""
        status = tasks_repo.get(self._conn, task_id).status
        self.settle_recovery()
        timer_repo.start(self._conn, task_id, self._now())
        self._changed()
        if status != TaskStatus.IN_PROGRESS:
            self._bus.publish(Changed(Entity.TASK, frozenset({task_id})))

    def pause(self) -> None:
        self.settle_recovery()
        timer_repo.pause(self._conn, self._now())
        self._changed()

    def pause_for_meeting(self, meeting_key: str) -> bool:
        """Auto-pause at a meeting's start (PLAN §5.5). True if a running timer paused."""
        self.settle_recovery()
        if self.running_task() is None:
            return False
        timer_repo.pause(self._conn, self._now(), PauseReason.MEETING, meeting_key)
        self._changed()
        return True

    def resume(self) -> None:
        timer_repo.resume(self._conn, self._now())
        self._changed()

    def stop(self) -> None:
        self.settle_recovery()
        timer_repo.stop(self._conn, self._now())
        self._changed()

    def toggle(self, task_id: int) -> None:
        """Space (SPEC §3): pause the task's running timer, resume it if it is paused,
        otherwise start it."""
        state = self.state()
        if state is not None and state.task_id == task_id:
            if state.running:
                self.pause()
            else:
                self.resume()
            return
        self.start(task_id)

    # Recovery ---------------------------------------------------------------------------

    def pending_recovery(self) -> Recovery | None:
        return self._recovery

    def recover(self, choice: RecoveryChoice) -> None:
        if self._recovery is None:
            return  # already settled by something else; the dialog is out of date
        timer_repo.recover(self._conn, choice, self._now())
        self._recovery = None
        self._changed()

    def settle_recovery(self) -> None:
        """Anything that changes the timer while the recovery question is still open
        answers it first, as "stop at last activity": the hours Daybook wasn't running
        must not be closed at *now* and logged as work."""
        if self._recovery is None:
            return
        log.info("settling the pending timer recovery at the last heartbeat")
        timer_repo.recover(self._conn, RecoveryChoice.STOP_AT_LAST_SEEN, self._now())
        self._recovery = None

    def data_replaced(self) -> None:
        """After a restore or import: a timer that was running in that data stops at its
        last heartbeat (nothing was watching it), and what is shown is re-read."""
        self._recovery = None
        state = timer_repo.get(self._conn)
        if state is not None and state.running:
            timer_repo.recover(self._conn, RecoveryChoice.STOP_AT_LAST_SEEN, self._now())
        self._changed()

    def discard_away(self, away_start: datetime) -> int:
        """Throw away the time since `away_start` on the running timer (D-042). Returns the
        seconds discarded."""
        self.settle_recovery()
        seconds = timer_repo.discard_away(self._conn, away_start, self._now())
        self._changed()
        return seconds

    # Heartbeat and lifecycle ------------------------------------------------------------

    def beat(self) -> None:
        if self._recovery is None:
            timer_repo.heartbeat(self._conn, self._now())

    def start_heartbeat(self) -> None:
        if self._source_id is not None:
            return
        # With fast dev time a fake minute is a real second (DAYBOOK_TIME_SCALE).
        seconds = self._clock.real_seconds_until(self._clock.now() + HEARTBEAT)
        self._source_id = GLib.timeout_add(max(250, int(seconds * 1000)), self._on_beat)

    def stop_heartbeat(self) -> None:
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None

    def _on_beat(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the heartbeat for good.
        try:
            self.beat()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_CONTINUE

    def shutdown(self) -> None:
        """A normal quit pauses a running timer, so the next start asks nothing (PLAN §5.6).
        An unanswered recovery is left for the next start."""
        self.stop_heartbeat()
        state = self.state()
        if self._recovery is None and state is not None and state.running:
            timer_repo.pause(self._conn, self._now())
            log.info("paused the timer on quit")

    # Manual entries ---------------------------------------------------------------------

    def entries(self, task_id: int) -> list[TimeEntry]:
        return time_entries.for_task(self._conn, task_id)

    def last_activity(self, day: date) -> datetime | None:
        """When timer time last ended on `day`, for drift nudges (None: not yet today)."""
        state = self.state()
        if state is not None and state.running:
            return self._clock.now()
        entries = time_entries.on_day(self._conn, day, self._clock.local_tz())
        ends = [e.end for e in entries]
        if state is not None:
            ends.append(state.last_seen)  # paused: when it was paused
        today_start, _ = timeutil.local_day_bounds(day, self._clock.local_tz())
        latest = max(ends, default=None)
        return latest if latest is not None and latest >= today_start else None

    def day_runs(self, day: date) -> list[Run]:
        """Today's tracked time as timeline blocks, the running timer included."""
        tz = self._clock.local_tz()
        lo, hi = timeutil.local_day_bounds(day, tz)
        entries = time_entries.on_day(self._conn, day, tz)
        return tracking.runs(entries, self.state(), lo, hi, self._clock.now())

    def overlap_owner(self, error: OverlapError) -> str:
        """The task an overlap is with, for the form message."""
        try:
            task = tasks_repo.get(self._conn, error.task_id, include_deleted=True)
        except NotFoundError:
            return ""
        return task.ticket_key or short(task.title)

    def add_entry(self, task_id: int, typed: EntryInput) -> int:
        start, end = tracking.to_interval(typed, self._clock.local_tz())
        entry_id = time_entries.add_manual(
            self._conn, task_id, start, end, note=typed.note, now=self._clock.now()
        )
        self._entries_changed(task_id)
        return entry_id

    def update_entry(self, entry_id: int, typed: EntryInput) -> None:
        start, end = tracking.to_interval(typed, self._clock.local_tz())
        before = time_entries.update(
            self._conn, entry_id, start, end, note=typed.note, now=self._clock.now()
        )
        self._entries_changed(before.task_id)

    def delete_entry(self, entry_id: int) -> None:
        removed = time_entries.delete(self._conn, entry_id)
        self._entries_changed(removed.task_id)
        self._bus.publish(
            Toast(
                _("Removed {time} of logged time").format(
                    time=timeutil.fmt_duration(removed.seconds // 60)
                ),
                undo=self._undo(removed),
            )
        )

    def _undo(self, removed: TimeEntry) -> Callable[[], None]:
        def undo() -> None:
            try:
                time_entries.reinsert(self._conn, removed)
            except NotFoundError:
                self._bus.publish(Toast(_("Couldn't undo: the task no longer exists")))
                return
            except ValidationError:
                self._bus.publish(Toast(_("Couldn't undo: other time was logged there since")))
                return
            self._entries_changed(removed.task_id)

        return undo

    # Events -----------------------------------------------------------------------------

    def _entries_changed(self, task_id: int) -> None:
        self._bus.publish(Changed(Entity.TIME_ENTRY, frozenset({task_id})))

    def _changed(self) -> None:
        self._bus.publish(Changed(Entity.TIMER))
        self._bus.publish(Changed(Entity.TIME_ENTRY))
