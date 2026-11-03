"""Tasks: validate → one repository transaction → publish `Changed`.

Complete, delete, hold and move publish a `Toast` whose Undo restores the task as it was
(D-039). Lists are loaded on the worker thread so a large database never stalls the UI.
"""

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from datetime import date, timedelta
from gettext import gettext as _

from daybook.core import hold as hold_rules
from daybook.core import reschedule, suggest
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.hold import HoldRequest
from daybook.core.models import (
    ChecklistItem,
    PlanItem,
    SidebarCounts,
    Task,
    TaskDraft,
    TaskStatus,
    TaskSummary,
    Tracked,
)
from daybook.core.repo import plan as plan_repo
from daybook.core.repo import tasks as tasks_repo
from daybook.core.repo import time_entries as time_repo
from daybook.core.reschedule import MoveChoice
from daybook.core.tasklists import UPCOMING_DAYS, TaskQuery
from daybook.core.timeutil import fmt_day
from daybook.services.calendar import CalendarService
from daybook.services.events import Changed, Entity, EventBus, Toast
from daybook.services.text import short
from daybook.services.worker import Worker

log = logging.getLogger(__name__)

# Soft-deleted tasks stay restorable this long, then they are purged at startup (D-017).
PURGE_AFTER = timedelta(hours=24)


class TaskService:
    def __init__(
        self,
        conn: Connection,
        bus: EventBus,
        clock: Clock,
        worker: Worker,
        calendar: CalendarService,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._worker = worker
        self._calendar = calendar

    # Time -------------------------------------------------------------------------------

    def today(self) -> date:
        return self._clock.now().astimezone(self._clock.local_tz()).date()

    # Reading ----------------------------------------------------------------------------

    def get(self, task_id: int) -> Task:
        return tasks_repo.get(self._conn, task_id)

    def plan_items(self, task_id: int) -> list[PlanItem]:
        """The task's plan from today on."""
        return plan_repo.items_for_task(self._conn, task_id, self.today())

    def tracked(self, task_id: int) -> Tracked:
        return time_repo.tracked(
            self._conn, task_id, self.today(), self._clock.local_tz(), self._clock.now()
        )

    def logged_seconds(self, task_id: int) -> int:
        return time_repo.total_seconds(self._conn, task_id)

    def load(
        self, query: TaskQuery, on_done: Callable[[list[TaskSummary]], None]
    ) -> Future[list[TaskSummary]]:
        return self._worker.submit(lambda conn: tasks_repo.list_tasks(conn, query), on_done)

    def load_counts(self, on_done: Callable[[SidebarCounts], None]) -> Future[SidebarCounts]:
        today = self.today()
        horizon = today + timedelta(days=UPCOMING_DAYS)
        return self._worker.submit(lambda conn: tasks_repo.counts(conn, today, horizon), on_done)

    def tags(self, task_id: int) -> tuple[str, ...]:
        return tasks_repo.tags_of(self._conn, task_id)

    def all_tags(self) -> list[str]:
        return tasks_repo.all_tags(self._conn)

    def requested_by_names(self) -> list[str]:
        return tasks_repo.requested_by_values(self._conn)

    def requested_by_suggestions(self, typed: str) -> list[str]:
        return suggest.matching(tasks_repo.requested_by_values(self._conn), typed)

    def tag_suggestions(self, typed: str) -> list[str]:
        return suggest.matching(tasks_repo.all_tags(self._conn), typed.lstrip("#"))

    def ticket_key_in_use(self, key: str, exclude_id: int | None = None) -> bool:
        return tasks_repo.ticket_key_in_use(self._conn, key, exclude_id)

    def checklist(self, task_id: int) -> list[ChecklistItem]:
        return tasks_repo.checklist(self._conn, task_id)

    # Creating and editing ---------------------------------------------------------------

    def create(self, draft: TaskDraft) -> int:
        if draft.plan is not None and draft.plan.day < self.today():
            raise ValidationError("plan", _("Plan for today or a later day."))
        task_id = tasks_repo.create(self._conn, draft, self._clock.now())
        self._changed(task_id, plan=draft.plan is not None)
        return task_id

    def update(self, task: Task) -> Task:
        saved = tasks_repo.update(self._conn, task, self._clock.now())
        self._changed(task.id)
        return saved

    def set_tags(self, task_id: int, names: Sequence[str]) -> None:
        tasks_repo.set_tags(self._conn, task_id, names)
        self._changed(task_id)

    # Status -----------------------------------------------------------------------------

    def set_status(self, task_id: int, status: TaskStatus) -> None:
        """To do / In progress directly; Done goes through `complete` (with Undo). On hold
        needs a reason, so callers use `put_on_hold`."""
        if status == TaskStatus.DONE:
            self.complete(task_id)
            return
        if status == TaskStatus.ON_HOLD:
            raise ValueError("use put_on_hold() with a reason")
        tasks_repo.set_status(self._conn, task_id, status, self._clock.now())
        self._changed(task_id)

    def complete(self, task_id: int) -> None:
        state = self._capture(task_id)
        if state.task.status == TaskStatus.DONE:
            return
        tasks_repo.complete(self._conn, task_id, self._clock.now())
        self._changed(task_id)
        self._toast(_("Completed “{title}”").format(title=short(state.task.title)), state)

    def reopen(self, task_id: int) -> None:
        tasks_repo.set_status(self._conn, task_id, TaskStatus.TO_DO, self._clock.now())
        self._changed(task_id)

    def put_on_hold(self, task_id: int, request: HoldRequest) -> None:
        """Hold with a reason and take it out of today's and later plans (D-046)."""
        today = self.today()
        clean = hold_rules.validate(request, today)
        state = self._capture(task_id)
        removed = tasks_repo.put_on_hold(
            self._conn,
            task_id,
            hold_rules.started(clean, self._clock.now()),
            today,
            self._clock.now(),
        )
        self._changed(task_id, plan=bool(removed))
        self._toast(_("“{title}” is on hold").format(title=short(state.task.title)), state)

    def resume(self, task_id: int) -> None:
        """Leave On hold: back to In progress (D-046)."""
        tasks_repo.set_status(self._conn, task_id, TaskStatus.IN_PROGRESS, self._clock.now())
        self._changed(task_id)

    # Moving -----------------------------------------------------------------------------

    def quick_move_choice(self) -> MoveChoice:
        return reschedule.quick_choice(self.today(), self._calendar.calendar())

    def move_target(self, choice: MoveChoice) -> date | None:
        """Resolve a choice to a date (None = unplanned). Raises ValidationError."""
        return reschedule.resolve(choice, self.today(), self._calendar.calendar())

    def move(self, task_id: int, choice: MoveChoice, reason: str | None = None) -> date | None:
        today = self.today()
        target = self.move_target(choice)
        task = self.get(task_id)
        if task.status in (TaskStatus.DONE, TaskStatus.ON_HOLD):
            raise ValidationError("move", _("Only open tasks can be moved."))
        note = reschedule.note_line(today, target, reason)
        state = self._capture(task_id)
        tasks_repo.move(self._conn, task_id, today, target, note=note, now=self._clock.now())
        self._changed(task_id, plan=True)
        if target is None:
            message = _("“{title}” is unplanned").format(title=short(task.title))
        else:
            message = _("Moved “{title}” to {day}").format(
                title=short(task.title), day=fmt_day(target, today)
            )
        self._toast(message, state)
        return target

    # Deleting ---------------------------------------------------------------------------

    def delete(self, task_id: int) -> None:
        """Soft delete with Undo (D-017). The UI confirms first when time was logged."""
        state = self._capture(task_id)
        tasks_repo.soft_delete(self._conn, task_id, self._clock.now())
        self._changed(task_id, plan=True)
        self._toast(_("Deleted “{title}”").format(title=short(state.task.title)), state)

    def purge_deleted(self) -> int:
        purged = tasks_repo.purge_deleted(self._conn, self._clock.now() - PURGE_AFTER)
        if purged:
            log.info("purged %d task(s) deleted more than a day ago", purged)
        return purged

    # Checklist --------------------------------------------------------------------------

    def add_checklist_item(self, task_id: int, text: str) -> int:
        item_id = tasks_repo.add_checklist_item(self._conn, task_id, text)
        self._changed(task_id)
        return item_id

    def set_checklist_done(self, item_id: int, done: bool) -> None:
        item = tasks_repo.set_checklist_done(self._conn, item_id, done)
        self._changed(item.task_id)

    def rename_checklist_item(self, item_id: int, text: str) -> None:
        item = tasks_repo.rename_checklist_item(self._conn, item_id, text)
        self._changed(item.task_id)

    def delete_checklist_item(self, item_id: int) -> None:
        item = tasks_repo.delete_checklist_item(self._conn, item_id)
        self._changed(item.task_id)

        def undo() -> None:
            try:
                tasks_repo.restore_checklist_item(self._conn, item)
            except NotFoundError:
                self._bus.publish(Toast(_("Couldn't undo: the task no longer exists")))
                return
            self._changed(item.task_id)

        self._bus.publish(Toast(_("Removed “{text}”").format(text=short(item.text)), undo=undo))

    # Helpers ----------------------------------------------------------------------------

    def _capture(self, task_id: int) -> tasks_repo.TaskState:
        return tasks_repo.capture(self._conn, task_id, self.today())

    def _toast(self, message: str, state: tasks_repo.TaskState) -> None:
        self._bus.publish(Toast(message, undo=lambda: self._undo(state)))

    def _undo(self, state: tasks_repo.TaskState) -> None:
        try:
            tasks_repo.restore(self._conn, state)
        except NotFoundError:
            self._bus.publish(Toast(_("Couldn't undo: the task no longer exists")))
            return
        self._changed(state.task.id, plan=True)

    def _changed(self, task_id: int, *, plan: bool = False) -> None:
        self._bus.publish(Changed(Entity.TASK, frozenset({task_id})))
        if plan:
            # Plan ids aren't known here; an empty set means "anything in the plan".
            self._bus.publish(Changed(Entity.PLAN))
