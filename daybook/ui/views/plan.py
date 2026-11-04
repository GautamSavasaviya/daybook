"""Plan my day (SPEC §2, A3, A8; PLAN §6.3): suggestions on the left, the plan on the right.

Give a suggestion a time (15m / 30m / 1h / 2h / Rest of day / other) to add it. Reorder the
plan by dragging or with Alt+Up/Down. Items past the end of the day sit below a "doesn't
fit" divider, which can move them to the next working day. Below 640 sp the suggestions
slide in from the side ("Add Tasks").
"""

from collections.abc import Callable
from datetime import date
from gettext import gettext as _

from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango

from daybook.core.models import TaskStatus, TaskSummary
from daybook.core.tasklists import CandidateReason, ListKind, TaskQuery, candidate_reason
from daybook.core.timeutil import fmt_day, fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.plan import DayPlan, PlanEntry
from daybook.services.safe import report_error, safe_callback
from daybook.ui.widgets.allotment import (
    PRESETS,
    AllotmentButton,
    allotment_label,
    short_label,
)
from daybook.ui.widgets.capacity_bar import CapacityBar
from daybook.ui.widgets.date_button import capitalised_day
from daybook.ui.widgets.project_dot import dot

NARROW = "max-width: 640sp"
_REFRESH_ON = (Entity.PLAN, Entity.TASK, Entity.MEETING, Entity.TIME_OFF, Entity.SETTINGS)


def reason_label(reason: CandidateReason) -> str:
    match reason:
        case CandidateReason.OVERDUE:
            return _("overdue")
        case CandidateReason.DUE:
            return _("due")
        case CandidateReason.FOLLOW_UP:
            return _("follow up")
        case CandidateReason.PRIORITY:
            return _("high priority")


def _suggestions_hint() -> str:
    return _("Overdue and due tasks, follow-ups and P1/P2 tasks from the Inbox appear here.")


def _title(summary: TaskSummary) -> str:
    task = summary.task
    return f"{task.ticket_key}  {task.title}" if task.ticket_key else task.title


class PlanView(Adw.BreakpointBin):
    def __init__(
        self,
        context: AppContext,
        *,
        on_open_task: Callable[[int], None],
        on_start_day: Callable[[], None],
        on_show_inbox: Callable[[], None],
    ) -> None:
        super().__init__(width_request=360, height_request=360)
        self._ctx = context
        self._on_open_task = on_open_task
        self._on_start_day = on_start_day
        self._on_show_inbox = on_show_inbox
        self._queued = False
        self._generation = 0
        self._search_text = ""
        self._row_items: dict[Gtk.ListBoxRow, PlanEntry] = {}
        self._candidate_rows: dict[Gtk.ListBoxRow, int] = {}
        self._subscriptions: list[Subscription] = []

        top = Gtk.Box(spacing=8, margin_top=12, margin_start=12, margin_end=12)
        self._days = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self._days.add(Adw.Toggle(name="today", label=_("Today")))
        self._days.add(Adw.Toggle(name="next", label=_("Tomorrow")))
        self._days.set_active_name("today")
        self._days.connect("notify::active-name", self._on_day_toggled)
        self._add_button = Gtk.ToggleButton(
            label=_("_Add Tasks"), use_underline=True, visible=False
        )
        spacer = Gtk.Box(hexpand=True)
        self._start_button = Gtk.Button(
            label=_("_Start Day"), use_underline=True, css_classes=["suggested-action"]
        )
        self._start_button.set_tooltip_text(_("Go to Today"))
        self._start_button.connect("clicked", self._on_start_clicked)
        for widget in (self._days, self._add_button, spacer, self._start_button):
            top.append(widget)

        self._capacity = CapacityBar()
        self._capacity.set_margin_start(12)
        self._capacity.set_margin_end(12)

        self._split = Adw.OverlaySplitView(
            sidebar=self._build_candidates(),
            content=self._build_plan(),
            min_sidebar_width=280,
            max_sidebar_width=380,
            vexpand=True,
        )
        self._split.bind_property(
            "show-sidebar",
            self._add_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for part in (top, self._capacity, self._split):
            box.append(part)
        self.set_child(box)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW))
        narrow.add_setter(self._split, "collapsed", True)
        narrow.add_setter(self._split, "show-sidebar", False)
        narrow.add_setter(self._add_button, "visible", True)
        self.add_breakpoint(narrow)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("map", self._on_map)
        self.connect("destroy", self._on_destroy)

    # Building ---------------------------------------------------------------------------

    def _build_candidates(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_start=12,
            margin_end=12,
            margin_bottom=12,
        )
        self._candidates_title = Gtk.Label(xalign=0, css_classes=["heading"])
        self._search = Gtk.SearchEntry(placeholder_text=_("Find any open task"))
        self._search.update_property([Gtk.AccessibleProperty.LABEL], [_("Find a task to plan")])
        self._search.connect("search-changed", self._on_search_changed)
        self._candidates = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        self._candidates.update_property([Gtk.AccessibleProperty.LABEL], [_("Suggestions")])
        self._candidates.connect("row-activated", self._on_candidate_activated)
        self._no_candidates = Adw.StatusPage(
            icon_name="daybook-inbox-symbolic",
            title=_("Nothing to Suggest"),
            description=_suggestions_hint(),
            css_classes=["compact"],
            vexpand=True,
        )
        inbox = Gtk.Button(label=_("Show Inbox"), halign=Gtk.Align.CENTER, css_classes=["pill"])
        inbox.connect("clicked", self._on_inbox_clicked)
        self._no_candidates.set_child(inbox)
        self._candidates_stack = Gtk.Stack(vexpand=True)
        self._candidates_stack.add_named(
            Gtk.ScrolledWindow(child=self._candidates, hscrollbar_policy=Gtk.PolicyType.NEVER),
            "list",
        )
        self._candidates_stack.add_named(self._no_candidates, "empty")
        for widget in (self._candidates_title, self._search, self._candidates_stack):
            box.append(widget)
        return box

    def _build_plan(self) -> Gtk.Widget:
        self._plan_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        self._plan_list.update_property([Gtk.AccessibleProperty.LABEL], [_("The day's plan")])
        self._plan_list.connect("row-activated", self._on_plan_activated)
        hint = Gtk.Label(
            label=_("Drag to reorder, or Alt+↑ / Alt+↓"),
            xalign=0,
            css_classes=["caption", "dim-label"],
        )
        list_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_start=12,
            margin_end=12,
            margin_bottom=24,
        )
        list_box.append(self._plan_list)
        list_box.append(hint)
        self._no_plan = Adw.StatusPage(icon_name="view-list-ordered-symbolic", vexpand=True)
        self._plan_stack = Gtk.Stack(vexpand=True)
        self._plan_stack.add_named(
            Gtk.ScrolledWindow(
                child=Adw.Clamp(child=list_box, maximum_size=700),
                hscrollbar_policy=Gtk.PolicyType.NEVER,
            ),
            "list",
        )
        self._plan_stack.add_named(self._no_plan, "empty")
        return self._plan_stack

    # Refresh ----------------------------------------------------------------------------

    def day(self) -> date:
        if self._days.get_active_name() == "next":
            return self._ctx.plan.next_day()
        return self._ctx.plan.today()

    def task_for_row(self, widget: Gtk.Widget) -> int | None:
        """The task of a focused plan row (Space and Ctrl+Enter act on it)."""
        entry = self._row_items.get(widget) if isinstance(widget, Gtk.ListBoxRow) else None
        return entry.summary.task.id if entry else None

    def show_today(self) -> None:
        self._days.set_active_name("today")

    def queue_refresh(self) -> None:
        if not self.get_mapped() or self._queued:
            return
        self._queued = True
        GLib.idle_add(self._refresh)

    @safe_callback
    def _refresh(self) -> bool:
        self._queued = False
        day = self.day()
        today = self._ctx.plan.today()
        next_toggle = self._days.get_toggle_by_name("next")
        if next_toggle is not None:
            next_toggle.set_label(capitalised_day(self._ctx.plan.next_day(), today))
        is_today = day == today
        self._start_button.set_visible(is_today)
        when = _("today") if is_today else fmt_day(day, today)
        self._candidates_title.set_text(_("Add to {day}").format(day=when))
        plan = self._ctx.plan.day_plan(day)
        self._capacity.show_schedule(plan.schedule)
        self._fill_plan(plan, when)
        self._load_candidates(day)
        return GLib.SOURCE_REMOVE

    def _fill_plan(self, plan: DayPlan, when: str) -> None:
        self._plan_list.remove_all()
        self._row_items.clear()
        if not plan.entries:
            self._no_plan.set_title(_("Nothing Planned"))
            self._no_plan.set_description(
                GLib.markup_escape_text(
                    _(
                        "Add tasks for {day} from the suggestions, or with Move to… on any task."
                    ).format(day=when)
                )
            )
            self._plan_stack.set_visible_child_name("empty")
            return
        self._plan_stack.set_visible_child_name("list")
        overflowing = plan.schedule.overflowing_ids()
        overflow_minutes = sum(o.minutes or 0 for o in plan.schedule.overflow)
        divider_shown = False
        for index, entry in enumerate(plan.entries, start=1):
            if entry.item.id in overflowing and not divider_shown and plan.schedule.working:
                self._plan_list.append(self._divider(plan.day, overflow_minutes))
                divider_shown = True
            self._plan_list.append(self._plan_row(index, entry, plan))

    def _divider(self, day: date, minutes: int) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        label = Gtk.Label(
            label=_("Doesn't fit ({time})").format(time=fmt_duration(minutes)),
            xalign=0,
            hexpand=True,
            css_classes=["warning", "heading"],
        )
        move = Gtk.Button(label=_("Move to Next Day"), valign=Gtk.Align.CENTER)
        move.connect("clicked", self._on_move_overflow, day)
        box.append(label)
        box.append(move)
        row = Gtk.ListBoxRow(child=box, activatable=False, selectable=False)
        row.update_property([Gtk.AccessibleProperty.LABEL], [label.get_text()])
        return row

    def _plan_row(self, index: int, entry: PlanEntry, plan: DayPlan) -> Gtk.ListBoxRow:
        summary = entry.summary
        task = summary.task
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=6)
        number = Gtk.Label(label=str(index), width_chars=2, css_classes=["numeric", "dim-label"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(
            label=_title(summary), xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=False
        )
        text.append(title)
        bits = [summary.project_name]
        if task.status == TaskStatus.DONE:
            bits.append(_("done"))
            title.add_css_class("dim-label")
        if entry.item.carry_count:
            bits.append(_("carried ×{n}").format(n=entry.item.carry_count))
        if task.moved_count >= 2:
            bits.append(_("moved ×{n}").format(n=task.moved_count))
        if task.requested_by:
            bits.append(_("from {name}").format(name=task.requested_by))
        if entry.item.allotted_min is None:
            share = plan.schedule.rest_of_day_min.get(entry.item.id)
            if share is not None:
                bits.append(_("{time} free for it").format(time=fmt_duration(share)))
        elif entry.tracked_min:
            bits.append(_("{time} tracked").format(time=fmt_duration(entry.tracked_min)))
        text.append(
            Gtk.Label(
                label=" · ".join(bits),
                xalign=0,
                wrap=True,
                use_markup=False,
                css_classes=["caption", "dim-label"],
            )
        )
        item_id = entry.item.id
        allotment = AllotmentButton(
            lambda minutes: self._ctx.plan.set_allotment(item_id, minutes),
            lambda: self._ctx.plan.remove(item_id),
        )
        allotment.show_minutes(entry.item.allotted_min)
        for widget in (number, dot(summary.project_color), text, allotment):
            box.append(widget)
        row = Gtk.ListBoxRow(child=box)
        row.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"{index}. {_title(summary)}, {allotment_label(entry.item.allotted_min)}"],
        )
        self._row_items[row] = entry
        self._add_drag_and_keys(row, item_id)
        return row

    def _add_drag_and_keys(self, row: Gtk.ListBoxRow, item_id: int) -> None:
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", self._on_drag_prepare, item_id)
        row.add_controller(source)
        # The stubs want a Python type here; GTK takes the GType itself.
        target = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)  # type: ignore[arg-type]
        target.connect("drop", self._on_drop, row)
        row.add_controller(target)
        keys = Gtk.ShortcutController()
        for trigger, delta in (("<Alt>Up", -1), ("<Alt>Down", 1)):
            keys.add_shortcut(
                Gtk.Shortcut(
                    trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                    action=Gtk.CallbackAction.new(self._move_shortcut(item_id, delta)),
                )
            )
        row.add_controller(keys)

    def _move_shortcut(
        self, item_id: int, delta: int
    ) -> Callable[[Gtk.Widget, GLib.Variant | None], bool]:
        def move(_widget: Gtk.Widget, _args: GLib.Variant | None) -> bool:
            # Not @safe_callback: shortcut callbacks must return a bool.
            try:
                self._ctx.plan.move_by(item_id, delta)
                GLib.idle_add(self._refocus, item_id)
            except Exception as exc:
                report_error(exc)
            return True

        return move

    @safe_callback
    def _refocus(self, item_id: int) -> bool:
        # Keep the keyboard on the moved item after the list is rebuilt.
        self._refresh()
        for row, entry in self._row_items.items():
            if entry.item.id == item_id:
                row.grab_focus()
        return GLib.SOURCE_REMOVE

    def _load_candidates(self, day: date) -> None:
        self._generation += 1
        generation = self._generation
        if self._search_text.strip():
            query = TaskQuery(ListKind.SEARCH, self._ctx.plan.today(), text=self._search_text)
            self._ctx.tasks.load(
                query, lambda result: self._fill_candidates(generation, day, result)
            )
        else:
            self._ctx.plan.load_candidates(
                day, lambda result: self._fill_candidates(generation, day, result)
            )

    def _fill_candidates(self, generation: int, day: date, result: list[TaskSummary]) -> None:
        if generation != self._generation:
            return
        planned = {entry.summary.task.id for entry in self._row_items.values()}
        searching = bool(self._search_text.strip())
        shown = [
            s
            for s in result
            if s.task.id not in planned
            and s.task.status != TaskStatus.DONE
            and not s.project_archived
        ]
        self._candidates.remove_all()
        self._candidate_rows.clear()
        for summary in shown:
            self._candidates.append(self._candidate_row(summary, day, searching))
        if shown:
            self._candidates_stack.set_visible_child_name("list")
        else:
            if searching:
                self._no_candidates.set_title(_("No Matching Tasks"))
                self._no_candidates.set_description(_("Only open tasks can be planned."))
            else:
                self._no_candidates.set_title(_("Nothing to Suggest"))
                self._no_candidates.set_description(_suggestions_hint())
            self._candidates_stack.set_visible_child_name("empty")

    def _candidate_row(self, summary: TaskSummary, day: date, searching: bool) -> Gtk.ListBoxRow:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        line = Gtk.Box(spacing=8)
        line.append(dot(summary.project_color))
        line.append(
            Gtk.Label(
                label=_title(summary),
                xalign=0,
                hexpand=True,
                ellipsize=Pango.EllipsizeMode.END,
                use_markup=False,
            )
        )
        box.append(line)
        if not searching:
            reason = reason_label(candidate_reason(summary, day))
            box.append(
                Gtk.Label(
                    label=f"{summary.project_name} · {reason}",
                    xalign=0,
                    css_classes=["caption", "dim-label"],
                    use_markup=False,
                )
            )
        presets = Adw.WrapBox(child_spacing=2, line_spacing=2)
        task_id = summary.task.id
        for minutes in (*PRESETS, None):
            button = Gtk.Button(label=short_label(minutes), css_classes=["flat", "preset"])
            button.set_tooltip_text(_("Plan with {time}").format(time=allotment_label(minutes)))
            button.connect("clicked", self._on_preset, task_id, minutes)
            presets.append(button)
        other = AllotmentButton(lambda minutes: self._add(task_id, minutes))
        other.set_icon_name("view-more-horizontal-symbolic")
        other.add_css_class("preset")
        other.set_tooltip_text(_("Plan with another time"))
        other.update_property([Gtk.AccessibleProperty.LABEL], [_("Plan with another time")])
        presets.append(other)
        box.append(presets)
        row = Gtk.ListBoxRow(child=box)
        row.update_property([Gtk.AccessibleProperty.LABEL], [_title(summary)])
        self._candidate_rows[row] = task_id
        return row

    def _add(self, task_id: int, minutes: int | None) -> None:
        self._ctx.plan.add(task_id, self.day(), minutes)

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_preset(self, _button: Gtk.Button, task_id: int, minutes: int | None) -> None:
        self._add(task_id, minutes)

    @safe_callback
    def _on_drag_prepare(
        self, _source: Gtk.DragSource, _x: float, _y: float, item_id: int
    ) -> Gdk.ContentProvider:
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_INT, item_id))

    @safe_callback
    def _on_drop(
        self, _target: Gtk.DropTarget, value: int, _x: float, _y: float, row: Gtk.ListBoxRow
    ) -> bool:
        entry = self._row_items.get(row)
        if entry is None:
            return False
        order = [e.item.id for e in self._row_items.values()]
        self._ctx.plan.move_to(int(value), order.index(entry.item.id))
        return True

    @safe_callback
    def _on_plan_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        entry = self._row_items.get(row)
        if entry is not None:
            self._on_open_task(entry.summary.task.id)

    @safe_callback
    def _on_candidate_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task_id = self._candidate_rows.get(row)
        if task_id is not None:
            self._on_open_task(task_id)

    @safe_callback
    def _on_move_overflow(self, _button: Gtk.Button, day: date) -> None:
        self._ctx.plan.move_overflow(day)

    @safe_callback
    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text()
        self._load_candidates(self.day())

    @safe_callback
    def _on_day_toggled(self, _group: Adw.ToggleGroup, _param: object) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        self._on_start_day()

    @safe_callback
    def _on_inbox_clicked(self, _button: Gtk.Button) -> None:
        self._on_show_inbox()

    def _on_changed(self, event: Changed) -> None:
        if event.entity in _REFRESH_ON:
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
