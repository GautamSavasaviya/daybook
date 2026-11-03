"""Review (PLAN §6.3, minimal until Phase 6): how the day went, opened by "Wrap up".

Each of today's planned tasks with the time tracked against its allotment; unfinished
ones carry over to the next working day on their own (D-045).
"""

from collections.abc import Callable
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, GLib, Gtk

from daybook.core.models import TaskStatus
from daybook.core.timeutil import fmt_day, fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Subscription
from daybook.services.plan import PlanEntry
from daybook.services.safe import safe_callback
from daybook.ui.widgets.date_button import capitalised_day
from daybook.ui.widgets.project_dot import dot


class ReviewView(Gtk.Box):
    def __init__(
        self,
        context: AppContext,
        *,
        on_open_task: Callable[[int], None],
        on_plan_next: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._ctx = context
        self._on_open_task = on_open_task
        self._on_plan_next = on_plan_next
        self._queued = False
        self._row_tasks: dict[Gtk.ListBoxRow, int] = {}
        self._subscriptions: list[Subscription] = []

        self._page = Adw.PreferencesPage(vexpand=True)
        self._summary = Adw.PreferencesGroup()
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._list.update_property([Gtk.AccessibleProperty.LABEL], [_("Today's plan")])
        self._list.connect("row-activated", self._on_row_activated)
        self._summary.add(self._list)
        self._page.add(self._summary)
        actions = Adw.PreferencesGroup()
        self._plan_next = Adw.ButtonRow(title=_("Plan the Next Working Day"))
        self._plan_next.connect("activated", self._on_plan_next_activated)
        actions.add(self._plan_next)
        self._page.add(actions)

        self._empty = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title=_("Nothing Was Planned Today"),
            description=_("Plan the next working day so it starts with a clear list."),
            vexpand=True,
        )
        empty_button = Gtk.Button(
            label=_("Plan Next Day"), halign=Gtk.Align.CENTER, css_classes=["pill"]
        )
        empty_button.connect("clicked", self._on_empty_clicked)
        self._empty.set_child(empty_button)
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
        tracked_s = sum(run.seconds for run in self._ctx.timer.day_runs(today))
        done = sum(1 for e in entries if not e.open)
        self._summary.set_title(GLib.markup_escape_text(capitalised_day(today, today)))
        self._summary.set_description(
            GLib.markup_escape_text(
                ngettext(
                    "{done} of {n} planned task done · {time} tracked",
                    "{done} of {n} planned tasks done · {time} tracked",
                    len(entries),
                ).format(done=done, n=len(entries), time=fmt_duration(tracked_s // 60))
            )
        )
        self._list.remove_all()
        self._row_tasks.clear()
        next_day = self._ctx.plan.next_day()
        for entry in entries:
            self._list.append(self._row(entry, fmt_day(next_day, today)))
        self._plan_next.set_title(_("Plan {day}").format(day=capitalised_day(next_day, today)))
        self._stack.set_visible_child_name("review" if entries else "empty")
        return GLib.SOURCE_REMOVE

    def _row(self, entry: PlanEntry, next_day: str) -> Gtk.ListBoxRow:
        task = entry.summary.task
        title = f"{task.ticket_key}  {task.title}" if task.ticket_key else task.title
        allotted = entry.item.allotted_min
        tracked = fmt_duration(entry.tracked_min)
        spent = (
            _("{tracked} tracked").format(tracked=tracked)
            if allotted is None
            else _("{tracked} of {allotted}").format(
                tracked=tracked, allotted=fmt_duration(allotted)
            )
        )
        if entry.open:
            status = _("carries over to {day}").format(day=next_day)
        elif entry.summary.task.status == TaskStatus.DONE:
            status = _("done")
        else:
            status = _("on hold")
        row = Adw.ActionRow(
            title=title, subtitle=f"{spent} · {status}", use_markup=False, activatable=True
        )
        row.add_prefix(dot(entry.summary.project_color))
        if not entry.open:
            row.add_suffix(
                Gtk.Image(
                    icon_name="object-select-symbolic",
                    accessible_role=Gtk.AccessibleRole.PRESENTATION,
                )
            )
        self._row_tasks[row] = task.id
        return row

    @safe_callback
    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task_id = self._row_tasks.get(row)
        if task_id is not None:
            self._on_open_task(task_id)

    @safe_callback
    def _on_plan_next_activated(self, _row: Adw.ButtonRow) -> None:
        self._on_plan_next()

    @safe_callback
    def _on_empty_clicked(self, _button: Gtk.Button) -> None:
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
