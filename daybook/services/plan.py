"""The day plan: what is planned, in which order, and how it fits the day (PLAN §5.1)."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import date, datetime
from gettext import gettext as _
from gettext import ngettext

from daybook.core import meetings as meeting_rules
from daybook.core import planner, reschedule, validation
from daybook.core.calendar import Interval
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import ChangedSinceError, NotFoundError
from daybook.core.models import Occurrence, PlanItem, TaskStatus, TaskSummary
from daybook.core.repo import plan as plan_repo
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_entries as time_repo
from daybook.core.repo import timer as timer_repo
from daybook.core.tasklists import ListKind, TaskQuery
from daybook.core.timeutil import fmt_day
from daybook.services.calendar import CalendarService
from daybook.services.events import Changed, Entity, EventBus, Toast
from daybook.services.meetings import MeetingService
from daybook.services.tasks import TaskService
from daybook.services.text import short
from daybook.services.worker import Worker

_OPEN = (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS)


@dataclass(frozen=True)
class PlanEntry:
    item: PlanItem
    summary: TaskSummary
    tracked_min: int  # on the plan's day

    @property
    def open(self) -> bool:
        return self.summary.task.status in _OPEN


@dataclass(frozen=True)
class DayPlan:
    day: date
    entries: tuple[PlanEntry, ...]
    occurrences: tuple[Occurrence, ...]
    schedule: planner.Schedule
    window: Interval | None
    lunch: Interval | None
    leave: Interval | None

    def entry(self, plan_item_id: int | None) -> PlanEntry | None:
        return next((e for e in self.entries if e.item.id == plan_item_id), None)


class PlanService:
    def __init__(
        self,
        conn: Connection,
        bus: EventBus,
        clock: Clock,
        *,
        worker: Worker,
        calendar: CalendarService,
        meetings: MeetingService,
        tasks: TaskService,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._worker = worker
        self._calendar = calendar
        self._meetings = meetings
        self._tasks = tasks

    # Days -------------------------------------------------------------------------------

    def today(self) -> date:
        return self._tasks.today()

    def next_day(self) -> date:
        """ "Tomorrow" in the Plan view: the next working day (D-051)."""
        return self._calendar.calendar().next_working_day(self.today())

    def is_working(self, day: date) -> bool:
        return self._calendar.calendar().is_working(day)

    # Reading ----------------------------------------------------------------------------

    def entries(self, day: date) -> list[PlanEntry]:
        items = plan_repo.items_on(self._conn, day)
        ids = [item.task_id for item in items]
        summaries = tasks_repo.summaries(self._conn, ids, self.today())
        tracked = time_repo.tracked_on_day(
            self._conn, ids, day, self._clock.local_tz(), self._clock.now()
        )
        return [
            PlanEntry(item, summaries[item.task_id], tracked.get(item.task_id, 0))
            for item in items
            if item.task_id in summaries
        ]

    def first_open(self, day: date) -> PlanEntry | None:
        """The first unfinished item in the day's plan ("Start Day" times it, D-084)."""
        return next((e for e in self.entries(day) if e.open), None)

    def day_plan(self, day: date, now: datetime | None = None) -> DayPlan:
        """The plan for `day` fitted around its meetings and lunch, from `now` on (pass the
        caller's `now` to compare the schedule with that same moment)."""
        tz = self._clock.local_tz()
        now = now or self._clock.now()
        calendar = self._calendar.calendar()
        entries = self.entries(day)
        occurrences = self._meetings.day_occurrences(day)
        window = calendar.day_window(day, tz)
        lunch = calendar.lunch(day, tz)
        busy = [
            planner.Busy(o.start, meeting_rules.effective_end(o, now), o.key, o.title)
            for o in occurrences
        ]
        inputs = [
            planner.PlanInput(e.item.id, e.item.task_id, e.item.allotted_min, e.tracked_min)
            for e in entries
            if e.open  # done and on-hold tasks take no time (D-046)
        ]
        running = timer_repo.get(self._conn)
        if running is not None and running.running and day == self.today():
            # What is being worked on is what's on now (D-091).
            inputs.sort(key=lambda i: i.task_id != running.task_id)
        return DayPlan(
            day=day,
            entries=tuple(entries),
            occurrences=tuple(occurrences),
            schedule=planner.schedule(window, lunch, busy, inputs, now),
            window=window,
            lunch=lunch,
            leave=calendar.leave(day, tz),
        )

    def load_candidates(
        self, day: date, on_done: Callable[[list[TaskSummary]], None]
    ) -> Future[list[TaskSummary]]:
        query = TaskQuery(ListKind.CANDIDATES, self.today(), plan_day=day)
        return self._worker.submit(lambda conn: tasks_repo.list_tasks(conn, query), on_done)

    # Changing ---------------------------------------------------------------------------

    def add(self, task_id: int, day: date, allotted_min: int | None) -> None:
        """Plan a task on `day`. A follow-up that is on hold resumes (D-046)."""
        reschedule.check_plan_day(day, self.today(), self._calendar.calendar(), "plan")
        validation.allotment(allotted_min)
        task = self._tasks.get(task_id)
        if task.status == TaskStatus.ON_HOLD:
            self._tasks.resume(task_id)
        elif task.status == TaskStatus.DONE:
            return
        plan_repo.add(self._conn, task_id, day, allotted_min)
        self._changed(task_id)

    def set_allotment(self, item_id: int, allotted_min: int | None) -> None:
        item = plan_repo.set_allotment(self._conn, item_id, validation.allotment(allotted_min))
        self._changed(item.task_id)

    def remove(self, item_id: int) -> None:
        title = short(self._tasks.get(plan_repo.get(self._conn, item_id).task_id).title)
        item = plan_repo.remove_item(self._conn, item_id)
        self._changed(item.task_id)

        def undo() -> None:
            try:
                plan_repo.reinsert(self._conn, item)
            except NotFoundError, ChangedSinceError:
                self._bus.publish(Toast(_("Couldn't undo: the plan has changed since")))
                return
            self._changed(item.task_id)

        self._bus.publish(
            Toast(_("Removed “{title}” from the plan").format(title=title), undo=undo)
        )

    def move_by(self, item_id: int, delta: int) -> None:
        """Move an item up (-1) or down (+1) in its day (Alt+Up/Down)."""
        item = plan_repo.get(self._conn, item_id)
        order = [i.id for i in plan_repo.items_on(self._conn, item.plan_date)]
        index = order.index(item_id)
        self.move_to(item_id, max(0, min(len(order) - 1, index + delta)))

    def move_to(self, item_id: int, index: int) -> None:
        """Put an item at `index` in its day (drag and drop)."""
        item = plan_repo.get(self._conn, item_id)
        order = [i.id for i in plan_repo.items_on(self._conn, item.plan_date) if i.id != item_id]
        order.insert(max(0, min(len(order), index)), item_id)
        plan_repo.reorder(self._conn, item.plan_date, order)
        self._changed(item.task_id)

    def move_overflow(self, day: date) -> int:
        """Move the items that don't fit `day` to the next working day, with Undo."""
        plan = self.day_plan(day)
        ids = [o.plan_item_id for o in plan.schedule.overflow]
        if not ids:
            return 0
        target = self._calendar.calendar().next_working_day(day)
        plan_from = min(day, self.today())
        task_ids = sorted({o.task_id for o in plan.schedule.overflow})
        before = [tasks_repo.capture(self._conn, tid, plan_from) for tid in task_ids]
        plan_repo.move_items(self._conn, ids, target)
        after = [tasks_repo.capture(self._conn, tid, plan_from) for tid in task_ids]
        self._changed(None)

        def undo() -> None:
            try:
                tasks_repo.restore_many(self._conn, list(zip(before, after, strict=True)))
            except NotFoundError, ChangedSinceError:
                self._bus.publish(Toast(_("Couldn't undo: the plan has changed since")))
                return
            self._changed(None)

        message = ngettext("Moved {n} task to {day}", "Moved {n} tasks to {day}", len(ids)).format(
            n=len(ids), day=fmt_day(target, self.today())
        )
        self._bus.publish(Toast(message, undo=undo))
        return len(ids)

    def _changed(self, task_id: int | None) -> None:
        self._bus.publish(Changed(Entity.PLAN))
        ids = frozenset() if task_id is None else frozenset({task_id})
        self._bus.publish(Changed(Entity.TASK, ids))
