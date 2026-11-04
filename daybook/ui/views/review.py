"""Review (SPEC §6, PLAN §6.3): how the day went, and what happens to what is unfinished.

Done tasks with the time they took; unfinished ones each with *Tomorrow*, *Inbox* and
*Put on hold…*, or all at once. They carry over to the next working day by themselves
(D-045); choosing Tomorrow instead counts as a move (D-047). Opened by "Wrap up".
"""

from collections.abc import Callable
from datetime import date
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, GLib, Gtk

from daybook.core.models import TaskStatus
from daybook.core.reschedule import MoveChoice, MoveKind
from daybook.core.timeutil import fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Subscription, Toast
from daybook.services.plan import PlanEntry
from daybook.services.safe import safe_callback
from daybook.ui.dialogs.hold import HoldDialog
from daybook.ui.widgets.buttons import Kind, button_bar, empty_page_button, text_button
from daybook.ui.widgets.date_button import capitalised_day
from daybook.ui.widgets.project_dot import dot


def _title(entry: PlanEntry) -> str:
    task = entry.summary.task
    return f"{task.ticket_key}  {task.title}" if task.ticket_key else task.title


class ReviewView(Gtk.Box):
    def __init__(
        self,
        context: AppContext,
        *,
        on_open_task: Callable[[int], None],
        on_plan_next: Callable[[], None],
        on_finish: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._ctx = context
        self._on_open_task = on_open_task
        self._on_plan_next = on_plan_next
        self._on_finish = on_finish
        self._queued = False
        self._groups: list[Adw.PreferencesGroup] = []
        self._row_tasks: dict[Gtk.ListBoxRow, int] = {}
        self._open_ids: list[int] = []
        self._subscriptions: list[Subscription] = []

        self._page = Adw.PreferencesPage(vexpand=True)
        self._empty = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title=_("Nothing Was Planned Today"),
            description=_("Plan the next working day so it starts with a clear list."),
            vexpand=True,
        )
        self._empty.set_child(empty_page_button(_("Plan Next Day"), self._on_plan_clicked))
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._page, "review")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("map", self._on_map)
        self.connect("destroy", self._on_destroy)

    def queue_refresh(self) -> None:
        if self.get_mapped() and not self._queued:
            self._queued = True
            GLib.idle_add(self._refresh)

    @safe_callback
    def _refresh(self) -> bool:
        self._queued = False
        today = self._ctx.tasks.today()
        entries = self._ctx.plan.entries(today)
        for group in self._groups:
            self._page.remove(group)
        self._groups.clear()
        self._row_tasks.clear()
        self._stack.set_visible_child_name("review" if entries else "empty")
        if not entries:
            return GLib.SOURCE_REMOVE
        next_day = self._ctx.plan.next_day()
        done = [e for e in entries if e.summary.task.status == TaskStatus.DONE]
        open_entries = [e for e in entries if e.open]
        held = [e for e in entries if e.summary.task.status == TaskStatus.ON_HOLD]
        self._open_ids = [e.summary.task.id for e in open_entries]
        tracked_s = sum(run.seconds for run in self._ctx.timer.day_runs(today))
        self._add(self._summary(today, entries, done, tracked_s))
        if done:
            self._add(self._done_group(done))
        if open_entries:
            self._add(self._open_group(open_entries, next_day, today))
        if held:
            self._add(self._held_group(held))
        self._add(self._finish_group(next_day, today))
        return GLib.SOURCE_REMOVE

    def _add(self, group: Adw.PreferencesGroup) -> None:
        self._page.add(group)
        self._groups.append(group)

    # Groups -----------------------------------------------------------------------------

    def _summary(
        self, today: date, entries: list[PlanEntry], done: list[PlanEntry], tracked_s: int
    ) -> Adw.PreferencesGroup:
        text = ngettext(
            "{done} of {n} planned task done · {time} tracked",
            "{done} of {n} planned tasks done · {time} tracked",
            len(entries),
        ).format(done=len(done), n=len(entries), time=fmt_duration(tracked_s // 60))
        return Adw.PreferencesGroup(
            title=GLib.markup_escape_text(capitalised_day(today, today)),
            description=GLib.markup_escape_text(text),
        )

    def _done_group(self, done: list[PlanEntry]) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Done"))
        for entry in done:
            row = self._row(entry, self._spent(entry))
            row.add_suffix(
                Gtk.Image(
                    icon_name="object-select-symbolic",
                    css_classes=["success"],
                    accessible_role=Gtk.AccessibleRole.PRESENTATION,
                )
            )
            group.add(row)
        return group

    def _open_group(
        self, open_entries: list[PlanEntry], next_day: date, today: date
    ) -> Adw.PreferencesGroup:
        day = capitalised_day(next_day, today)
        group = Adw.PreferencesGroup(
            title=_("Not Finished"),
            description=GLib.markup_escape_text(
                _("These carry over to {day} on their own. Or decide now:").format(day=day)
            ),
        )
        for entry in open_entries:
            task_id = entry.summary.task.id
            row = self._row(entry, f"{self._spent(entry)} · " + _("carries over"))
            row.add_suffix(self._buttons(task_id))
            group.add(row)
        everything = text_button(
            _("Move All to {day}").format(day=day),
            lambda _b: self._move_all(MoveKind.NEXT_WORKING_DAY),
        )
        inbox = text_button(
            _("Move All Back to Inbox"), lambda _b: self._move_all(MoveKind.UNPLANNED)
        )
        group.add(button_bar(everything, inbox))
        return group

    def _held_group(self, held: list[PlanEntry]) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("On Hold"), description=_("Put on hold today; they left the plan.")
        )
        for entry in held:
            group.add(self._row(entry, self._spent(entry)))
        return group

    def _finish_group(self, next_day: date, today: date) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        plan = text_button(
            _("Plan {day}").format(day=capitalised_day(next_day, today)),
            lambda _b: self._on_plan_activated(),
        )
        finish = text_button(
            _("Finish Day"), lambda _b: self._on_finish_clicked(), kind=Kind.SUGGESTED
        )
        group.add(button_bar(plan, finish))
        return group

    # Rows -------------------------------------------------------------------------------

    def _spent(self, entry: PlanEntry) -> str:
        allotted = entry.item.allotted_min
        tracked = fmt_duration(entry.tracked_min)
        if allotted is None:
            return _("{tracked} tracked").format(tracked=tracked)
        return _("{tracked} of {allotted}").format(tracked=tracked, allotted=fmt_duration(allotted))

    def _row(self, entry: PlanEntry, subtitle: str) -> Adw.ActionRow:
        row = Adw.ActionRow(
            title=_title(entry),
            subtitle=subtitle,
            use_markup=False,
            title_lines=0,
            activatable=True,
        )
        row.add_prefix(dot(entry.summary.project_color))
        row.connect("activated", self._on_row_activated, entry.summary.task.id)
        return row

    def _buttons(self, task_id: int) -> Gtk.Widget:
        box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        tomorrow = text_button(
            _("Tomorrow"),
            lambda _b: self._move_one(task_id, MoveKind.NEXT_WORKING_DAY),
            tooltip=_("Move to the next working day"),
        )
        inbox = text_button(
            _("Inbox"),
            lambda _b: self._move_one(task_id, MoveKind.UNPLANNED),
            tooltip=_("Take it out of the plan and put it back in the Inbox"),
        )
        hold = text_button(
            _("Hold…"), lambda b: self._on_hold(b, task_id), tooltip=_("Put on hold, with a reason")
        )
        for widget in (tomorrow, inbox, hold):
            box.append(widget)
        return box

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_row_activated(self, _row: Adw.ActionRow, task_id: int) -> None:
        self._on_open_task(task_id)

    @safe_callback
    def _move_one(self, task_id: int, kind: MoveKind) -> None:
        self._ctx.tasks.move_many([task_id], MoveChoice(kind))

    @safe_callback
    def _move_all(self, kind: MoveKind) -> None:
        self._ctx.tasks.move_many(self._open_ids, MoveChoice(kind))

    @safe_callback
    def _on_hold(self, button: Gtk.Button, task_id: int) -> None:
        root = button.get_root()
        if isinstance(root, Gtk.Window):
            HoldDialog(self._ctx, task_id).present(root)

    @safe_callback
    def _on_finish_clicked(self) -> None:
        count = len(self._open_ids)
        if count:
            day = capitalised_day(self._ctx.plan.next_day(), self._ctx.tasks.today())
            message = ngettext(
                "Day finished. {n} task carries over to {day}.",
                "Day finished. {n} tasks carry over to {day}.",
                count,
            ).format(n=count, day=day)
        else:
            message = _("Day finished. Nothing is left over.")
        self._ctx.bus.publish(Toast(message))
        self._on_finish()

    @safe_callback
    def _on_plan_activated(self) -> None:
        self._on_plan_next()

    @safe_callback
    def _on_plan_clicked(self, _button: Gtk.Button) -> None:
        self._on_plan_next()

    def _on_changed(self, _event: Changed) -> None:
        self.queue_refresh()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
