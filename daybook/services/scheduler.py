"""ReminderScheduler: the glue between `core/reminders.py`, the database and GNOME (§5.5).

One recompute reads a snapshot of the day, fires every reminder that became due since the
last check (each key is logged before it is sent, so nothing fires twice, D-024), turns
late ones into a single summary (D-027), withdraws reminders that became moot (D-054) and
arms one timeout for the next due time. It runs on that timeout (at least every 30 s, which
also catches wall-clock jumps), after every data change, after resume and at day change.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from gettext import gettext as _

from gi.repository import Gio, GLib

from daybook.core import reminders
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.reminders import Action, Kind, PlanLine, Reminder, Snapshot, View
from daybook.core.repo import reminders as log_repo
from daybook.core.repo.reminders import Status
from daybook.services.calendar import CalendarService
from daybook.services.clockformat import clock_format
from daybook.services.events import Changed, DayChanged, EventBus, ShowView, Toast
from daybook.services.meetings import MeetingService
from daybook.services.notifier import Notifier
from daybook.services.plan import PlanService
from daybook.services.safe import report_error
from daybook.services.settings import SettingsService
from daybook.services.tasks import TaskService
from daybook.services.text import short
from daybook.services.timer import TimerService

log = logging.getLogger(__name__)

MAX_WAIT_S = 30  # re-check at least this often (real seconds)
JUMP_S = 60  # a wall-clock jump bigger than this is logged as one (suspend, clock change)


class ReminderScheduler:
    def __init__(
        self,
        conn: Connection,
        bus: EventBus,
        clock: Clock,
        *,
        settings: SettingsService,
        calendar: CalendarService,
        tasks: TaskService,
        plan: PlanService,
        meetings: MeetingService,
        timer: TimerService,
        notifier: Notifier,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._settings = settings
        self._calendar = calendar
        self._tasks = tasks
        self._plan = plan
        self._meetings = meetings
        self._timer = timer
        self._notifier = notifier
        self._source_id: int | None = None
        self._queued = False
        self._running = False
        self._started_at: datetime | None = None
        self._previous_check: datetime | None = None
        self._last_clock: tuple[datetime, float] | None = None
        self._shown: dict[str, Reminder] = {}  # subject → the reminder showing for it
        bus.subscribe(Changed, self._on_changed)
        bus.subscribe(DayChanged, self._on_day_changed)

    # Lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        now = self._clock.now()
        self._previous_check = log_repo.watermark(self._conn)
        self._started_at = now
        if self._previous_check is None:
            # A new database: nothing was missed, so no summary (D-027).
            log_repo.set_watermark(self._conn, now)
        self._running = True
        self.recompute()

    def stop(self) -> None:
        self._running = False
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
            self._source_id = None

    @property
    def shown(self) -> dict[str, Reminder]:
        return dict(self._shown)

    # Reading the day --------------------------------------------------------------------

    def snapshot(self, now: datetime) -> Snapshot:
        tz = self._clock.local_tz()
        today = now.astimezone(tz).date()
        day = self._plan.day_plan(today, now)
        occurrences = {o.key: o for o in self._meetings.day_occurrences(today)}
        for occurrence in self._meetings.day_occurrences(today + timedelta(days=1)):
            occurrences.setdefault(occurrence.key, occurrence)
        state = self._timer.state()
        lines = []
        for entry in day.entries:
            task = entry.summary.task
            tracked_s = entry.tracked_min * 60
            if state is not None and state.task_id == task.id:
                tracked_s = self._timer.tracked_today_s(task.id)
            lines.append(
                PlanLine(
                    item_id=entry.item.id,
                    task_id=task.id,
                    name=task.ticket_key or short(task.title, 30),
                    allotted_min=entry.item.allotted_min,
                    tracked_s=tracked_s,
                    open=entry.open,
                    carried=entry.item.carry_count > 0,
                )
            )
        timer_name = ""
        if state is not None:
            try:
                task = self._tasks.get(state.task_id)
                timer_name = task.ticket_key or short(task.title, 30)
            except NotFoundError:
                timer_name = ""
        return Snapshot(
            today=today,
            tz=tz,
            settings=self._settings.current,
            calendar=self._calendar.calendar(),
            occurrences=tuple(sorted(occurrences.values(), key=lambda o: (o.start, o.key))),
            plan=tuple(lines),
            schedule=day.schedule,
            timer=state,
            timer_task_name=timer_name,
            idle_since=self._timer.last_activity(today),
            snoozes=tuple(log_repo.snoozes(self._conn)),
            started_at=self._started_at,
            previous_check=self._previous_check,
            follow_ups=self._tasks.follow_ups_due(),
        )

    # Recomputing ------------------------------------------------------------------------

    def recompute(self) -> None:
        if not self._running:
            return
        now = self._clock.now()
        self._note_clock_jump(now)
        snapshot = self.snapshot(now)
        candidates = reminders.compute(snapshot, now)
        since = log_repo.watermark(self._conn) or now
        # Due since the last check; "due now" (overflow, an overdue nudge) always counts.
        due = [r for r in candidates if since < r.due <= now or r.due == now]
        done = log_repo.logged(self._conn, [r.key for r in due])
        triage = reminders.triage([r for r in due if r.key not in done], now)
        for reminder in triage.fire:
            self._fire(reminder, now)
        # Snoozes that came due before the last check (the app was closed) are dropped.
        stale = [r for r in snapshot.snoozes if r.due <= since]
        log_repo.log_missed(self._conn, [*triage.missed, *stale], now)
        if triage.summary:
            tz = snapshot.tz
            summary = reminders.summary_reminder(triage.summary, now, tz, clock_format().use_24h)
            self._fire(summary, now)
        log_repo.set_watermark(self._conn, now)
        self._withdraw_moot(snapshot, now)
        upcoming = [r.due for r in candidates if r.due > now]
        self._arm(min(upcoming, default=None))

    def _fire(self, reminder: Reminder, now: datetime) -> None:
        if not log_repo.log(self._conn, reminder, Status.FIRED, now):
            return  # someone else logged it first: never twice (D-024)
        if reminder.kind == Kind.AUTOPAUSE:
            if reminder.occurrence is not None:
                self._timer.pause_for_meeting(reminder.occurrence)
            return
        try:
            message = reminders.render(
                reminder, now, self._clock.local_tz(), clock_format().use_24h
            )
            self._shown[reminder.subject] = reminder
            self._notifier.show(reminder, message)
        except Exception as exc:
            # It is logged as fired, so it won't be retried (at most once, D-024); say so.
            self._shown.pop(reminder.subject, None)
            log.error("could not show reminder %s", reminder.key, exc_info=exc)
            report_error(exc)

    def reset(self) -> None:
        """Forget and withdraw everything on screen (the data under it was replaced)."""
        for subject in list(self._shown):
            self._withdraw(subject)
        self._notifier.live.clear()

    def _withdraw_moot(self, snapshot: Snapshot, now: datetime) -> None:
        for subject, reminder in list(self._shown.items()):
            if not reminders.still_relevant(reminder, snapshot, now):
                self._withdraw(subject)

    def _withdraw(self, subject: str) -> None:
        if self._shown.pop(subject, None) is not None:
            self._notifier.withdraw(subject)

    def _note_clock_jump(self, now: datetime) -> None:
        mono = self._clock.monotonic()
        if self._last_clock is not None:
            wall, last_mono = self._last_clock
            jump = (now - wall).total_seconds() - (mono - last_mono)
            if abs(jump) > JUMP_S:
                # The watermark makes the reminders in the gap late, so they are summarised.
                log.info("the clock jumped by %+d s (suspend or a clock change)", jump)
        self._last_clock = (now, mono)

    def _arm(self, next_due: datetime | None) -> None:
        if self._source_id is not None:
            GLib.source_remove(self._source_id)
        wait = float(MAX_WAIT_S)
        if next_due is not None:
            wait = min(wait, self._clock.real_seconds_until(next_due))
        # A little slack so the reminder is really due when the timeout runs.
        self._source_id = GLib.timeout_add(max(50, int(wait * 1000) + 50), self._on_timeout)

    def _on_timeout(self) -> bool:
        # Not @safe_callback: a failure must not leave the scheduler without a timeout.
        self._source_id = None
        try:
            self.recompute()
        except Exception as exc:
            report_error(exc)
            self._arm(None)
        return GLib.SOURCE_REMOVE

    def queue_recompute(self) -> None:
        if self._running and not self._queued:
            self._queued = True
            GLib.idle_add(self._on_idle)

    def _on_idle(self) -> bool:
        self._queued = False
        try:
            self.recompute()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_REMOVE

    def _on_changed(self, _event: Changed) -> None:
        self.queue_recompute()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.queue_recompute()

    def resumed(self) -> None:
        """After suspend: whatever was due while asleep is summarised now."""
        log.info("resumed from suspend; checking reminders")
        self.queue_recompute()

    # Test reminder ----------------------------------------------------------------------

    def send_test(self) -> None:
        self._fire(reminders.test_reminder(self._clock.now()), self._clock.now())

    # Actions ----------------------------------------------------------------------------

    def _find(self, key: str) -> Reminder | None:
        for reminder in self._shown.values():
            if reminder.key == key:
                return reminder
        return log_repo.get(self._conn, key)

    def handle(self, key: str, action_name: str) -> None:
        """A notification button, the notification itself, or the in-app card."""
        reminder = self._find(key)
        try:
            action = Action(action_name)
        except ValueError:
            log.warning("unknown reminder action %r for %s", action_name, key)
            return
        if reminder is None:
            log.warning("reminder %s is unknown; ignoring %s", key, action)
            return
        now = self._clock.now()
        log_repo.record_action(self._conn, key, action, now)
        self._shown.pop(reminder.subject, None)
        self._notifier.withdraw(reminder.subject)  # also right after a restart (not in _shown)
        try:
            self._act(reminder, action, now)
        except ValidationError as error:
            self._bus.publish(Toast(error.message))
        except NotFoundError:
            self._bus.publish(Toast(_("That is no longer there")))
        self.queue_recompute()

    def _act(self, reminder: Reminder, action: Action, now: datetime) -> None:
        handlers: dict[Action, Callable[[Reminder, datetime], None]] = {
            Action.OPEN: self._open,
            Action.JOIN: self._join,
            Action.SNOOZE: self._snooze,
            Action.SWITCH: self._switch,
            Action.EXTEND: self._extend,
            Action.DONE: self._done,
            Action.START: self._start,
            Action.SKIP: self._skip,
            Action.RESUME: self._resume,
            Action.PICK: lambda _r, _n: self._show(View.TODAY),
            Action.MOVE_TOMORROW: self._move_tomorrow,
            Action.REVIEW_PLAN: lambda _r, _n: self._show(View.PLAN),
            Action.END_NOW: self._end_meeting,
            Action.END_AT_LAST: self._end_meeting,
        }
        handler = handlers.get(action)
        if handler is not None:  # Still in it, Works and Dismissed only close it
            handler(reminder, now)

    def _show(self, view: View) -> None:
        self._bus.publish(ShowView(view))

    def _open(self, reminder: Reminder, now: datetime) -> None:
        self._show(reminders.render(reminder, now, self._clock.local_tz(), True).view)

    def _join(self, reminder: Reminder, _now: datetime) -> None:
        if not reminder.link:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(reminder.link, None)
        except GLib.Error as exc:
            log.warning("could not open %s: %s", reminder.link, exc)
            self._bus.publish(Toast(_("Couldn't open the meeting link")))

    def _snooze(self, reminder: Reminder, now: datetime) -> None:
        base = reminder.key.split(":snooze")[0]
        count = log_repo.snooze_count(self._conn, base)
        log_repo.add_snooze(self._conn, reminders.snoozed(reminder, now, count))

    def _switch(self, reminder: Reminder, _now: datetime) -> None:
        if reminder.next_task_id is None:
            self._show(View.TODAY)
        else:
            self._timer.start(reminder.next_task_id)

    def _extend(self, reminder: Reminder, _now: datetime) -> None:
        if reminder.item_id is not None:
            minutes = (reminder.minutes or 0) + reminders.EXTEND_MIN
            self._plan.set_allotment(reminder.item_id, minutes)

    def _done(self, reminder: Reminder, _now: datetime) -> None:
        if reminder.task_id is not None:
            self._tasks.complete(reminder.task_id)

    def _start(self, reminder: Reminder, _now: datetime) -> None:
        if reminder.task_id is not None:
            self._timer.start(reminder.task_id)

    def _skip(self, reminder: Reminder, _now: datetime) -> None:
        """Drift "Skip": the item goes to the end of today's plan, so "planned now" moves
        on to the next one (D-025)."""
        if reminder.item_id is not None:
            last = len(self._plan.entries(self._tasks.today()))
            self._plan.move_to(reminder.item_id, last)

    def _move_tomorrow(self, _reminder: Reminder, _now: datetime) -> None:
        self._plan.move_overflow(self._tasks.today())

    def _resume(self, _reminder: Reminder, _now: datetime) -> None:
        state = self._timer.state()
        if state is not None and not state.running:
            self._timer.resume()

    def _end_meeting(self, reminder: Reminder, _now: datetime) -> None:
        if reminder.meeting_id is None:
            return
        if self._meetings.get(reminder.meeting_id).open_ended:
            self._meetings.end_now(reminder.meeting_id, reminder.last_seen)
