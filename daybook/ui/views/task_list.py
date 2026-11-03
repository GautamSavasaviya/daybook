"""The shared task list: Inbox, Upcoming, a project, Done and search results (D-051).

Rows are `TaskRow`s in a Gtk.ListView, grouped into sections by `core.tasklists`. Data is
loaded on the worker thread and the list re-queries whenever tasks, plans or projects
change, or the day changes. A hidden list only re-queries when it is shown again.
"""

from collections.abc import Callable
from datetime import date
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from daybook.core.models import Priority, TaskStatus, TaskSummary
from daybook.core.tasklists import (
    ListKind,
    Section,
    SectionKind,
    TaskFilter,
    TaskQuery,
    sections,
)
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.widgets.date_button import capitalised_day
from daybook.ui.widgets.task_row import TaskRow

EmptyAction = tuple[str, Callable[[], None]]


class TaskItem(GObject.Object):
    __gtype_name__ = "DaybookTaskItem"

    def __init__(self, summary: TaskSummary, section: int, section_title: str) -> None:
        super().__init__()
        self.summary = summary
        self.section = section
        self.section_title = section_title


def section_title(section: Section, today: date) -> str:
    if section.day is not None:
        return capitalised_day(section.day, today)
    titles = {
        SectionKind.OVERDUE: _("Overdue"),
        SectionKind.ON_HOLD: _("On hold"),
        SectionKind.IN_PROGRESS: _("In progress"),
        SectionKind.TO_DO: _("To do"),
        SectionKind.OPEN: _("Open"),
        SectionKind.DONE: _("Done"),
    }
    return titles.get(section.kind, "")


def status_label(status: TaskStatus) -> str:
    match status:
        case TaskStatus.TO_DO:
            return _("To do")
        case TaskStatus.IN_PROGRESS:
            return _("In progress")
        case TaskStatus.ON_HOLD:
            return _("On hold")
        case TaskStatus.DONE:
            return _("Done")


_STATUS_CHOICES = {
    ListKind.INBOX: (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS),
    ListKind.UPCOMING: (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS, TaskStatus.ON_HOLD),
    ListKind.PROJECT: (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS, TaskStatus.ON_HOLD),
    ListKind.DONE: (),
    ListKind.SEARCH: tuple(TaskStatus),
}


class _Choice:
    """A filter drop-down: "Any …" followed by values."""

    def __init__(self, any_label: str, accessible: str, on_change: Callable[[], None]) -> None:
        self._any = any_label
        self.values: list[object] = []
        self._loading = False
        self.dropdown = Gtk.DropDown(tooltip_text=accessible)
        self.dropdown.update_property([Gtk.AccessibleProperty.LABEL], [accessible])
        self.dropdown.connect("notify::selected", self._on_selected, on_change)

    def set_values(self, values: list[object], labels: list[str]) -> None:
        current = self.value
        self._loading = True
        try:
            self.values = values
            self.dropdown.set_model(Gtk.StringList.new([self._any, *labels]))
            index = values.index(current) + 1 if current in values else 0
            self.dropdown.set_selected(index)
        finally:
            self._loading = False

    @property
    def value(self) -> object | None:
        index = self.dropdown.get_selected()
        if 0 < index <= len(self.values):
            return self.values[index - 1]
        return None

    def reset(self) -> None:
        self.dropdown.set_selected(0)

    @safe_callback
    def _on_selected(
        self, _dropdown: Gtk.DropDown, _param: object, on_change: Callable[[], None]
    ) -> None:
        if not self._loading:
            on_change()


class FilterBar(Gtk.Revealer):
    """Project/area, status, priority, tag and requested-by filters (D-051)."""

    def __init__(
        self, context: AppContext, kind: ListKind, on_changed: Callable[[TaskFilter], None]
    ) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._ctx = context
        self._kind = kind
        self._on_changed = on_changed
        self._project = _Choice(_("Any project"), _("Filter by project or area"), self._emit)
        self._status = _Choice(_("Any status"), _("Filter by status"), self._emit)
        self._priority = _Choice(_("Any priority"), _("Filter by priority"), self._emit)
        self._tag = _Choice(_("Any tag"), _("Filter by tag"), self._emit)
        self._person = _Choice(_("Anyone"), _("Filter by who asked"), self._emit)

        box = Adw.WrapBox(
            child_spacing=6, line_spacing=6, margin_start=12, margin_end=12, margin_bottom=6
        )
        if kind != ListKind.PROJECT:
            box.append(self._project.dropdown)
        if _STATUS_CHOICES[kind]:
            box.append(self._status.dropdown)
            statuses = list(_STATUS_CHOICES[kind])
            self._status.set_values(list(statuses), [status_label(s) for s in statuses])
        box.append(self._priority.dropdown)
        priorities = list(Priority)
        self._priority.set_values(list(priorities), [f"P{p.value}" for p in priorities])
        box.append(self._tag.dropdown)
        box.append(self._person.dropdown)
        clear = Gtk.Button(label=_("Clear"), css_classes=["flat"])
        clear.connect("clicked", self._on_clear)
        box.append(clear)
        self.set_child(box)
        self.connect("notify::reveal-child", self._on_revealed)

    def reload_choices(self) -> None:
        archived = self._kind in (ListKind.DONE, ListKind.SEARCH)
        projects = self._ctx.projects.all(include_archived=archived)
        self._project.set_values([p.id for p in projects], [p.name for p in projects])
        tags = self._ctx.tasks.all_tags()
        self._tag.set_values(list(tags), [f"#{t}" for t in tags])
        people = self._ctx.tasks.requested_by_names()
        self._person.set_values(list(people), list(people))

    def current(self) -> TaskFilter:
        project = self._project.value
        status = self._status.value
        priority = self._priority.value
        tag = self._tag.value
        person = self._person.value
        return TaskFilter(
            project_id=project if isinstance(project, int) else None,
            status=status if isinstance(status, TaskStatus) else None,
            priority=priority if isinstance(priority, Priority) else None,
            tag=tag if isinstance(tag, str) else None,
            requested_by=person if isinstance(person, str) else None,
        )

    def clear(self) -> None:
        for choice in (self._project, self._status, self._priority, self._tag, self._person):
            choice.reset()

    def _emit(self) -> None:
        self._on_changed(self.current())

    @safe_callback
    def _on_clear(self, _button: Gtk.Button) -> None:
        self.clear()

    @safe_callback
    def _on_revealed(self, _revealer: Gtk.Revealer, _param: object) -> None:
        if self.get_reveal_child():
            self.reload_choices()


def _ordering(a: object, b: object, _data: object = None) -> int:
    # Sections are already in display order; the sorter only marks where each one starts.
    first = a.section if isinstance(a, TaskItem) else 0
    second = b.section if isinstance(b, TaskItem) else 0
    return (first > second) - (first < second)


class TaskListView(Gtk.Box):
    def __init__(
        self,
        context: AppContext,
        kind: ListKind,
        *,
        on_open: Callable[[int], None],
        on_new_task: Callable[[str], None],
        empty_action: EmptyAction,
        project_id: int | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._ctx = context
        self._kind = kind
        self._on_open = on_open
        self._on_new_task = on_new_task
        self._empty_action = empty_action
        self._project_id = project_id
        self._project_name = ""
        self._text = ""
        self._filter = TaskFilter()
        self._today = context.tasks.today()
        self._generation = 0
        self._dirty = True
        self._refresh_queued = False
        self._selected_id: int | None = None
        self._subscriptions: list[Subscription] = []

        self.filters = FilterBar(context, kind, self._on_filter_changed)
        self.append(self.filters)

        self._store = Gio.ListStore.new(TaskItem)
        sorted_model = Gtk.SortListModel(
            model=self._store, section_sorter=Gtk.CustomSorter.new(_ordering)
        )
        self._selection = Gtk.SingleSelection(
            model=sorted_model, autoselect=False, can_unselect=True
        )
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        self._list = Gtk.ListView(
            model=self._selection,
            factory=factory,
            show_separators=True,
            tab_behavior=Gtk.ListTabBehavior.ITEM,
        )
        self._list.add_css_class("background")
        self._list.update_property([Gtk.AccessibleProperty.LABEL], [_("Tasks")])
        if kind != ListKind.INBOX:
            headers = Gtk.SignalListItemFactory()
            headers.connect("setup", self._on_header_setup)
            headers.connect("bind", self._on_header_bind)
            self._list.set_header_factory(headers)
        self._list.connect("activate", self._on_activate)
        clamp = Adw.ClampScrollable(child=self._list, maximum_size=900, tightening_threshold=600)
        scroller = Gtk.ScrolledWindow(child=clamp, vexpand=True)

        self._empty = Adw.StatusPage(vexpand=True)
        self._empty_button = Gtk.Button(halign=Gtk.Align.CENTER, css_classes=["pill"])
        self._empty_button.connect("clicked", self._on_empty_action)
        self._empty.set_child(self._empty_button)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._stack.set_transition_duration(150)
        self._stack.add_named(scroller, "list")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)

        bus = context.bus
        self._subscriptions.append(bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("map", self._on_map)
        self.connect("destroy", self._on_destroy)
        self._show_empty_state()

    # Public -----------------------------------------------------------------------------

    @property
    def kind(self) -> ListKind:
        return self._kind

    def set_project(self, project_id: int, name: str) -> None:
        if project_id != self._project_id:
            self._store.remove_all()
            self.filters.clear()
        self._project_id = project_id
        self._project_name = name
        self.queue_refresh()

    def set_text(self, text: str) -> None:
        self._text = text
        self.queue_refresh()

    def set_selected_task(self, task_id: int | None) -> None:
        self._selected_id = task_id
        self._sync_selection()

    def queue_refresh(self) -> None:
        if not self.get_mapped():
            self._dirty = True
            return
        if not self._refresh_queued:
            self._refresh_queued = True
            GLib.idle_add(self._refresh)

    # Loading ----------------------------------------------------------------------------

    def _query(self) -> TaskQuery:
        return TaskQuery(
            self._kind,
            self._today,
            self._filter,
            project_id=self._project_id,
            text=self._text,
        )

    @safe_callback
    def _refresh(self) -> bool:
        self._refresh_queued = False
        self._dirty = False
        if self._kind == ListKind.PROJECT and self._project_id is None:
            return GLib.SOURCE_REMOVE
        self._today = self._ctx.tasks.today()
        self._generation += 1
        generation = self._generation
        self._ctx.tasks.load(self._query(), lambda result: self._apply(generation, result))
        return GLib.SOURCE_REMOVE

    def _apply(self, generation: int, result: list[TaskSummary]) -> None:
        if generation != self._generation:
            return  # a newer query is on its way
        items: list[TaskItem] = []
        for index, section in enumerate(
            sections(self._kind, result, self._today, self._ctx.clock.local_tz())
        ):
            title = section_title(section, self._today)
            items.extend(TaskItem(summary, index, title) for summary in section.items)
        self._store.splice(0, self._store.get_n_items(), items)
        if items:
            self._stack.set_visible_child_name("list")
        else:
            self._show_empty_state()
        self._sync_selection()

    def _sync_selection(self) -> None:
        model = self._selection.get_model()
        if model is None:
            return
        for position in range(model.get_n_items()):
            item = model.get_item(position)
            if isinstance(item, TaskItem) and item.summary.task.id == self._selected_id:
                self._selection.set_selected(position)
                return
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def _show_empty_state(self) -> None:
        page = self._empty
        label, _action = self._empty_action
        if self._kind == ListKind.SEARCH and not self._text.strip():
            page.set_icon_name("system-search-symbolic")
            page.set_title(_("Search Tasks"))
            page.set_description(
                _("Type to search titles, ticket keys and notes, done tasks included.")
            )
            self._empty_button.set_visible(False)
        elif self._filter.active:
            page.set_icon_name("daybook-filter-symbolic")
            page.set_title(_("No Matching Tasks"))
            page.set_description(_("No task matches these filters."))
            self._empty_button.set_label(_("Clear Filters"))
            self._empty_button.set_visible(True)
        elif self._kind == ListKind.SEARCH:
            page.set_icon_name("system-search-symbolic")
            page.set_title(_("No Results"))
            page.set_description(_("Nothing matches “{text}”.").format(text=self._text.strip()))
            self._empty_button.set_label(_("Create “{text}”").format(text=self._text.strip()))
            self._empty_button.set_visible(True)
        else:
            icon, title, description = self._empty_texts()
            page.set_icon_name(icon)
            page.set_title(title)
            page.set_description(description)
            self._empty_button.set_label(label)
            self._empty_button.set_visible(True)
        self._stack.set_visible_child_name("empty")

    def _empty_texts(self) -> tuple[str, str, str]:
        match self._kind:
            case ListKind.INBOX:
                return (
                    "daybook-inbox-symbolic",
                    _("Inbox Is Empty"),
                    _("Tasks you haven't planned yet collect here."),
                )
            case ListKind.UPCOMING:
                return (
                    "x-office-calendar-symbolic",
                    _("Nothing Coming Up"),
                    _(
                        "Tasks due or planned in the next two weeks, and tasks on hold, "
                        "appear here."
                    ),
                )
            case ListKind.PROJECT:
                return (
                    "folder-symbolic",
                    _("No Open Tasks"),
                    _("Tasks in {name} appear here. Done tasks are in Done.").format(
                        name=self._project_name
                    ),
                )
            case ListKind.DONE | ListKind.SEARCH:
                return (
                    "object-select-symbolic",
                    _("Nothing Done Yet"),
                    _("Completed tasks appear here and stay searchable."),
                )

    # List items -------------------------------------------------------------------------

    @safe_callback
    def _on_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        row = TaskRow(self._on_done_toggled)
        click = Gtk.GestureClick()
        click.connect("released", self._on_row_clicked, row)
        row.add_controller(click)
        list_item.set_child(row)

    @safe_callback
    def _on_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        row = list_item.get_child()
        if isinstance(item, TaskItem) and isinstance(row, TaskRow):
            row.bind(
                item.summary,
                self._today,
                self._ctx.clock.local_tz(),
                show_project=self._kind != ListKind.PROJECT,
            )

    @safe_callback
    def _on_header_setup(self, _factory: Gtk.SignalListItemFactory, header: Gtk.ListHeader) -> None:
        header.set_child(
            Gtk.Label(xalign=0, css_classes=["heading", "task-list-header"], use_markup=False)
        )

    @safe_callback
    def _on_header_bind(self, _factory: Gtk.SignalListItemFactory, header: Gtk.ListHeader) -> None:
        item = header.get_item()
        label = header.get_child()
        if isinstance(item, TaskItem) and isinstance(label, Gtk.Label):
            label.set_text(item.section_title)
            label.set_visible(bool(item.section_title))

    @safe_callback
    def _on_row_clicked(
        self,
        _gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        row: TaskRow,
    ) -> None:
        picked = row.pick(x, y, Gtk.PickFlags.DEFAULT)
        if isinstance(picked, Gtk.CheckButton) or (
            picked is not None and isinstance(picked.get_ancestor(Gtk.CheckButton), Gtk.CheckButton)
        ):
            return  # the done check handles its own click
        if row.task_id is not None:
            self._open(row.task_id)

    @safe_callback
    def _on_activate(self, _list: Gtk.ListView, position: int) -> None:
        model = self._selection.get_model()
        item = model.get_item(position) if model is not None else None
        if isinstance(item, TaskItem):
            self._open(item.summary.task.id)

    def _open(self, task_id: int) -> None:
        self.set_selected_task(task_id)
        self._on_open(task_id)

    def _on_done_toggled(self, task_id: int, done: bool) -> None:
        if done:
            self._ctx.tasks.complete(task_id)
        else:
            self._ctx.tasks.reopen(task_id)

    # Events -----------------------------------------------------------------------------

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.TASK, Entity.PLAN, Entity.PROJECT):
            self.queue_refresh()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.queue_refresh()

    def _on_filter_changed(self, task_filter: TaskFilter) -> None:
        self._filter = task_filter
        self.queue_refresh()

    @safe_callback
    def _on_empty_action(self, _button: Gtk.Button) -> None:
        if self._kind == ListKind.SEARCH and self._text.strip() and not self._filter.active:
            self._on_new_task(self._text.strip())
        elif self._filter.active:
            self.filters.clear()  # each reset drop-down reports the change
        else:
            self._empty_action[1]()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        if self._dirty:
            self.queue_refresh()

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
