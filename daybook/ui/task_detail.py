"""The task detail panel (PLAN §6.3): every field edits in place and saves on its own.

Shown beside the list on wide windows and over it on narrow ones. It re-reads the task
whenever the task changes, without overwriting a field the user is typing in.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from daybook.core import hold as hold_rules
from daybook.core import validation
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import ChecklistItem, Priority, Project, Recurrence, Task, TaskStatus
from daybook.core.timeutil import fmt_day, fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription, Toast
from daybook.services.safe import report_error, safe_callback
from daybook.ui.dialogs.hold import HoldDialog
from daybook.ui.dialogs.reschedule import MoveDialog
from daybook.ui.views.task_list import status_label
from daybook.ui.widgets.date_button import DateButton
from daybook.ui.widgets.repeat import RepeatRow
from daybook.ui.widgets.suggest import Suggestions

NOTES_SAVE_DELAY_MS = 800
_STATUSES = tuple(TaskStatus)


def _labelled_dropdown(labels: list[str], accessible: str) -> Gtk.DropDown:
    dropdown = Gtk.DropDown(model=Gtk.StringList.new(labels), tooltip_text=accessible)
    dropdown.update_property([Gtk.AccessibleProperty.LABEL], [accessible])
    return dropdown


def _icon_button(icon: str, label: str) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, tooltip_text=label, valign=Gtk.Align.CENTER)
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return button


class TaskDetail(Adw.Bin):
    def __init__(self, context: AppContext, *, on_close: Callable[[], None]) -> None:
        super().__init__()
        self._ctx = context
        self._on_close = on_close
        self._task_id: int | None = None
        self._task: Task | None = None
        self._projects: list[Project] = []
        self._checklist_key: tuple[tuple[int, str, bool], ...] | None = None
        self._loading = False
        self._notes_timeout: int | None = None
        self._subscriptions: list[Subscription] = []

        actions = Gio.SimpleActionGroup()
        delete = Gio.SimpleAction.new("delete", None)
        delete.connect("activate", self._on_delete)
        actions.add_action(delete)
        self.insert_action_group("detail", actions)

        view = Adw.ToolbarView()
        view.add_top_bar(self._build_header())
        view.set_content(self._build_body())
        self.set_child(view)

        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                action=Gtk.CallbackAction.new(self._on_escape),
            )
        )
        self.add_controller(shortcuts)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("destroy", self._on_destroy)

    # Building ---------------------------------------------------------------------------

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar(show_title=False)
        self._ticket_button = Gtk.Button(
            child=Adw.ButtonContent(icon_name="adw-external-link-symbolic"),
            tooltip_text=_("Open the ticket in your browser"),
            css_classes=["flat"],
        )
        self._ticket_button.connect("clicked", self._on_open_ticket)
        header.pack_start(self._ticket_button)

        close = _icon_button("window-close-symbolic", _("Close"))
        close.connect("clicked", self._on_close_clicked)
        header.pack_end(close)

        menu = Gio.Menu()
        menu.append(_("_Delete Task"), "detail.delete")
        more = Gtk.MenuButton(
            icon_name="view-more-symbolic", menu_model=menu, tooltip_text=_("More")
        )
        more.update_property([Gtk.AccessibleProperty.LABEL], [_("More")])
        header.pack_end(more)
        return header

    def _build_body(self) -> Gtk.Widget:
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=6,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )

        self._title = Gtk.EditableLabel(css_classes=["title-3"])
        self._title.update_property([Gtk.AccessibleProperty.LABEL], [_("Title")])
        self._title.connect("notify::editing", self._on_title_editing)
        self._title_error = self._error_label()
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.append(self._title)
        title_box.append(self._title_error)
        body.append(title_box)

        meta = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self._project = _labelled_dropdown([], _("Project or area"))
        self._project.set_enable_search(True)
        self._project.connect("notify::selected", self._on_project_selected)
        self._priority = _labelled_dropdown([f"P{p.value}" for p in Priority], _("Priority"))
        self._priority.connect("notify::selected", self._on_priority_selected)
        self._status = _labelled_dropdown([status_label(s) for s in _STATUSES], _("Status"))
        self._status.connect("notify::selected", self._on_status_selected)
        for widget in (self._project, self._priority, self._status):
            meta.append(widget)
        body.append(meta)

        self._hold_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card", "hold-card"]
        )
        self._hold_text = Gtk.Label(xalign=0, wrap=True, use_markup=False)
        resume = Gtk.Button(label=_("_Resume"), use_underline=True, halign=Gtk.Align.START)
        resume.connect("clicked", self._on_resume)
        self._hold_card.append(self._hold_text)
        self._hold_card.append(resume)
        body.append(self._hold_card)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._plan_label = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self._time_label = Gtk.Label(xalign=0, wrap=True, css_classes=["numeric"])
        self._time_bar = Gtk.LevelBar(min_value=0, max_value=1)
        self._time_bar.update_property([Gtk.AccessibleProperty.LABEL], [_("Time against estimate")])
        for info_widget in (self._plan_label, self._time_label, self._time_bar):
            info.append(info_widget)
        body.append(info)

        body.append(self._build_details())
        body.append(self._build_notes())
        body.append(self._build_checklist())
        body.append(self._build_actions())

        clamp = Adw.Clamp(child=body, maximum_size=560)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _error_label(self) -> Gtk.Label:
        return Gtk.Label(
            xalign=0,
            wrap=True,
            visible=False,
            css_classes=["field-error", "caption"],
            accessible_role=Gtk.AccessibleRole.ALERT,
        )

    def _build_details(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        group = Adw.PreferencesGroup()
        self._more = Adw.ExpanderRow(title=_("More details"))
        self._ticket = Adw.EntryRow(title=_("Ticket key"), show_apply_button=True)
        self._ticket.connect("apply", self._on_ticket_apply)
        self._due = DateButton(
            today=self._ctx.tasks.today,
            empty_label=_("None"),
            accessible_name=_("Due date"),
            on_changed=self._on_due_changed,
        )
        due_row = Adw.ActionRow(title=_("Due date"))
        due_row.add_suffix(self._due)
        self._repeat = RepeatRow(self._on_repeat_changed)
        self._estimate = Adw.EntryRow(title=_("Estimate, e.g. 2h"), show_apply_button=True)
        self._estimate.connect("apply", self._on_estimate_apply)
        self._requested_by = Adw.EntryRow(title=_("Requested by"), show_apply_button=True)
        self._requested_by.connect("apply", self._on_requested_by_apply)
        Suggestions(
            self._requested_by,
            self._ctx.tasks.requested_by_suggestions,
            lambda _name: self._save_requested_by(),
        )
        self._tags_row = Adw.ActionRow(title=_("Tags"))
        self._tags_box = Adw.WrapBox(child_spacing=4, line_spacing=4, valign=Gtk.Align.CENTER)
        self._tags_row.add_suffix(self._tags_box)
        self._new_tag = Adw.EntryRow(title=_("Add a tag"))
        self._new_tag.connect("entry-activated", self._on_new_tag)
        Suggestions(self._new_tag, self._ctx.tasks.tag_suggestions, lambda _tag: self._add_tag())
        self._field_rows: dict[str, Gtk.Widget] = {
            "ticket_key": self._ticket,
            "estimate": self._estimate,
            "requested_by": self._requested_by,
            "tags": self._new_tag,
        }
        for row in (
            self._ticket,
            due_row,
            self._repeat,
            self._estimate,
            self._requested_by,
            self._tags_row,
            self._new_tag,
        ):
            self._more.add_row(row)
        group.add(self._more)
        self._details_error = self._error_label()
        box.append(group)
        box.append(self._details_error)
        return box

    def _build_notes(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Notes"))
        self._notes = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
            accepts_tab=False,
        )
        self._notes.update_property([Gtk.AccessibleProperty.LABEL], [_("Notes")])
        self._notes.get_buffer().connect("changed", self._on_notes_changed)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", self._on_notes_leave)
        self._notes.add_controller(focus)
        frame = Gtk.ScrolledWindow(
            child=self._notes,
            min_content_height=96,
            max_content_height=320,
            propagate_natural_height=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            css_classes=["card"],
            overflow=Gtk.Overflow.HIDDEN,
        )
        group.add(frame)
        return group

    def _build_checklist(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title=_("Checklist"))
        self._checklist = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        self._checklist.update_property([Gtk.AccessibleProperty.LABEL], [_("Checklist")])
        self._new_item = Adw.EntryRow(title=_("Add an item"))
        self._new_item.connect("entry-activated", self._on_new_item)
        self._checklist.append(self._new_item)
        group.add(self._checklist)
        self._checklist_error = self._error_label()
        group.add(self._checklist_error)
        return group

    def _build_actions(self) -> Gtk.Widget:
        box = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self._hold_button = Gtk.Button(label=_("Put on _Hold…"), use_underline=True)
        self._hold_button.connect("clicked", self._on_hold_clicked)
        self._move_button = Gtk.Button(label=_("_Move To…"), use_underline=True)
        self._move_button.connect("clicked", self._on_move_clicked)
        self._done_button = Gtk.Button(use_underline=True, css_classes=["suggested-action"])
        self._done_button.connect("clicked", self._on_done_clicked)
        for widget in (self._done_button, self._hold_button, self._move_button):
            box.append(widget)
        return box

    # Loading ----------------------------------------------------------------------------

    @property
    def task_id(self) -> int | None:
        return self._task_id

    def show_task(self, task_id: int) -> None:
        switched = task_id != self._task_id
        if switched:
            # Finish edits for the task being left, while it is still the current one.
            if self._title.get_editing():
                self._title.stop_editing(True)
            self._flush_notes()
            self._checklist_key = None
            self._clear_errors()
            self._task_id = task_id
            self._more.set_expanded(False)
        self._reload(initial=switched)

    def flush(self) -> None:
        """Save notes still waiting for their delayed save (before closing or quitting)."""
        self._flush_notes()

    def _reload(self, *, initial: bool = False) -> None:
        if self._task_id is None:
            return
        try:
            task = self._ctx.tasks.get(self._task_id)
        except NotFoundError:
            self._task_id = None
            self._on_close()
            return
        self._task = task
        self._loading = True
        try:
            self._fill(task, initial=initial)
        finally:
            self._loading = False

    def _fill(self, task: Task, *, initial: bool) -> None:
        # `initial`: a different task was opened, so every field shows the new task, even
        # one that has focus; otherwise a field being typed in is left alone.
        if initial or not self._title.get_editing():
            self._title.set_text(task.title)

        url = self._ctx.projects.ticket_url(task)
        self._ticket_button.set_visible(bool(url))
        content = self._ticket_button.get_child()
        if isinstance(content, Adw.ButtonContent):
            content.set_label(task.ticket_key or "")

        projects = self._ctx.projects.all()
        if all(p.id != task.project_id for p in projects):
            # An archived project still shows for its own tasks.
            projects.append(self._ctx.projects.get(task.project_id))
        self._projects = projects
        self._project.set_model(Gtk.StringList.new([p.name for p in projects]))
        self._project.set_selected(
            next(i for i, p in enumerate(projects) if p.id == task.project_id)
        )
        self._priority.set_selected(task.priority.value - 1)
        self._status.set_selected(_STATUSES.index(task.status))
        today = self._ctx.tasks.today()
        self._fill_hold(task, today)
        self._fill_plan_and_time(task, today)
        self._fill_details(task, initial=initial)

        done = task.status == TaskStatus.DONE
        self._done_button.set_label(_("_Reopen") if done else _("Mark _Done"))
        self._hold_button.set_visible(task.status in (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS))
        self._move_button.set_visible(task.status in (TaskStatus.TO_DO, TaskStatus.IN_PROGRESS))

    def _fill_hold(self, task: Task, today: date) -> None:
        on_hold = task.status == TaskStatus.ON_HOLD and task.hold is not None
        self._hold_card.set_visible(on_hold)
        if task.hold is not None:
            days = hold_rules.days_waiting(task.hold, today, self._ctx.clock.local_tz())
            parts = [
                _("On hold: {reason}").format(reason=hold_rules.reason_label(task.hold.reason)),
                _("waiting {days}d").format(days=days),
            ]
            if task.hold.follow_up is not None:
                parts.append(_("follow up {day}").format(day=fmt_day(task.hold.follow_up, today)))
            text = " · ".join(parts)
            if task.hold.note:
                text += "\n" + task.hold.note
            self._hold_text.set_text(text)

    def _fill_plan_and_time(self, task: Task, today: date) -> None:
        items = self._ctx.tasks.plan_items(task.id)
        if items and task.status != TaskStatus.DONE:
            first = items[0]
            amount = (
                _("rest of day") if first.allotted_min is None else fmt_duration(first.allotted_min)
            )
            self._plan_label.set_text(
                _("Planned {day} · {amount}").format(
                    day=fmt_day(first.plan_date, today), amount=amount
                )
            )
        else:
            self._plan_label.set_text(_("Not planned"))

        tracked = self._ctx.tasks.tracked(task.id)
        text = _("{today} today · {total} total").format(
            today=fmt_duration(tracked.today_s // 60), total=fmt_duration(tracked.total_s // 60)
        )
        if task.estimate_min:
            text += " " + _("of {estimate} estimate").format(
                estimate=fmt_duration(task.estimate_min)
            )
            self._time_bar.set_value(min(1.0, tracked.total_s / 60 / task.estimate_min))
        self._time_bar.set_visible(bool(task.estimate_min))
        self._time_label.set_text(text)

    def _fill_details(self, task: Task, *, initial: bool) -> None:
        self._set_entry(self._ticket, task.ticket_key or "", force=initial)
        self._due.set_value(task.due_date)
        self._repeat.set_rule(task.recurrence, task.due_date or self._ctx.tasks.today())
        self._set_entry(
            self._estimate,
            fmt_duration(task.estimate_min) if task.estimate_min else "",
            force=initial,
        )
        self._set_entry(self._requested_by, task.requested_by or "", force=initial)
        if initial:
            self._new_tag.set_text("")
            self._new_item.set_text("")
        tags = self._ctx.tasks.tags(task.id)
        self._fill_tags(tags)
        if initial and (
            task.ticket_key
            or task.due_date
            or task.estimate_min
            or task.requested_by
            or tags
            or task.recurrence
        ):
            self._more.set_expanded(True)

        if initial or not self._notes.has_focus():
            buffer = self._notes.get_buffer()
            if buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) != task.notes:
                buffer.set_text(task.notes)

        self._fill_checklist(self._ctx.tasks.checklist(task.id))

    def _set_entry(self, row: Adw.EntryRow, text: str, *, force: bool = False) -> None:
        # Leave a field alone while the user is typing in it (unless the task changed).
        root = row.get_root()
        focus = root.get_focus() if isinstance(root, Gtk.Window) else None
        typing = focus is not None and focus.is_ancestor(row)
        if (force or not typing) and row.get_text() != text:
            row.set_text(text)

    def _fill_tags(self, tags: tuple[str, ...]) -> None:
        self._tags_box.remove_all()
        for tag in tags:
            chip = Gtk.Box(spacing=2, css_classes=["chip", "caption"])
            chip.append(Gtk.Label(label=f"#{tag}", use_markup=False))
            remove = Gtk.Button(
                icon_name="window-close-symbolic",
                css_classes=["flat", "circular"],
                tooltip_text=_("Remove tag {tag}").format(tag=tag),
            )
            remove.update_property(
                [Gtk.AccessibleProperty.LABEL], [_("Remove tag {tag}").format(tag=tag)]
            )
            remove.connect("clicked", self._on_remove_tag, tag)
            chip.append(remove)
            self._tags_box.append(chip)
        self._tags_row.set_subtitle("" if tags else _("None"))

    def _fill_checklist(self, items: list[ChecklistItem]) -> None:
        key = tuple((item.id, item.text, item.done) for item in items)
        if key == self._checklist_key:
            return  # unchanged: keep the rows (and any focus inside them)
        self._checklist_key = key
        while (
            row := self._checklist.get_row_at_index(0)
        ) is not None and row is not self._new_item:
            self._checklist.remove(row)
        for index, item in enumerate(items):
            self._checklist.insert(self._checklist_row(item), index)

    def _checklist_row(self, item: ChecklistItem) -> Gtk.Widget:
        row = Adw.EntryRow(text=item.text, show_apply_button=True)
        row.update_property([Gtk.AccessibleProperty.LABEL], [_("Checklist item")])
        check = Gtk.CheckButton(active=item.done, valign=Gtk.Align.CENTER)
        check.set_tooltip_text(_("Done"))
        check.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("“{text}” done").format(text=item.text)]
        )
        check.connect("toggled", self._on_item_toggled, item.id)
        row.add_prefix(check)
        remove = _icon_button("user-trash-symbolic", _("Remove item"))
        remove.add_css_class("flat")
        remove.connect("clicked", self._on_item_remove, item.id)
        row.add_suffix(remove)
        row.connect("apply", self._on_item_apply, item.id)
        return row

    # Saving -----------------------------------------------------------------------------

    def _save(self, change: Callable[[Task], Task], field: str | None = None) -> bool:
        if self._task_id is None:
            return False
        try:
            self._ctx.tasks.update(change(self._ctx.tasks.get(self._task_id)))
        except ValidationError as error:
            self._show_error(error, field)
            return False
        self._clear_errors()
        return True

    def _show_error(self, error: ValidationError, field: str | None = None) -> None:
        self._clear_errors()
        name = field or error.field
        if name == "title":
            label = self._title_error
        elif name == "checklist":
            label = self._checklist_error
        else:
            label = self._details_error
            self._more.set_expanded(True)
            row = self._field_rows.get(name)
            if row is not None:
                row.add_css_class("error")
        label.set_text(error.message)
        label.set_visible(True)

    def _clear_errors(self) -> None:
        for label in (self._title_error, self._details_error, self._checklist_error):
            label.set_visible(False)
        for row in self._field_rows.values():
            row.remove_css_class("error")

    def _flush_notes(self) -> None:
        if self._notes_timeout is not None:
            GLib.source_remove(self._notes_timeout)
            self._notes_timeout = None
            self._save_notes()

    def _save_notes(self) -> None:
        buffer = self._notes.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        if self._task is not None and text.rstrip() != self._task.notes:
            self._save(lambda task: replace(task, notes=text), "notes")

    def _save_requested_by(self) -> None:
        self._save(lambda task: replace(task, requested_by=self._requested_by.get_text()))

    def _add_tag(self) -> None:
        if self._task_id is None:
            return
        try:
            names = validation.split_tags(self._new_tag.get_text())
            if names:
                self._ctx.tasks.set_tags(
                    self._task_id, [*self._ctx.tasks.tags(self._task_id), *names]
                )
        except ValidationError as error:
            self._show_error(error)
            return
        self._new_tag.set_text("")
        self._clear_errors()

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_title_editing(self, label: Gtk.EditableLabel, _param: object) -> None:
        if self._loading or label.get_editing() or self._task is None:
            return
        if label.get_text() != self._task.title and not self._save(
            lambda task: replace(task, title=label.get_text()), "title"
        ):
            label.set_text(self._task.title)

    @safe_callback
    def _on_project_selected(self, dropdown: Gtk.DropDown, _param: object) -> None:
        index = dropdown.get_selected()
        if self._loading or index >= len(self._projects):
            return
        project_id = self._projects[index].id
        self._save(lambda task: replace(task, project_id=project_id), "project")

    @safe_callback
    def _on_priority_selected(self, dropdown: Gtk.DropDown, _param: object) -> None:
        if self._loading:
            return
        priority = Priority(dropdown.get_selected() + 1)
        self._save(lambda task: replace(task, priority=priority))

    @safe_callback
    def _on_status_selected(self, dropdown: Gtk.DropDown, _param: object) -> None:
        if self._loading or self._task_id is None:
            return
        status = _STATUSES[dropdown.get_selected()]
        if status == TaskStatus.ON_HOLD:
            self._open_hold_dialog()
        else:
            self._ctx.tasks.set_status(self._task_id, status)

    @safe_callback
    def _on_ticket_apply(self, row: Adw.EntryRow) -> None:
        self._save(lambda task: replace(task, ticket_key=row.get_text()), "ticket_key")

    @safe_callback
    def _on_due_changed(self, value: date | None) -> None:
        self._save(lambda task: replace(task, due_date=value))

    @safe_callback
    def _on_repeat_changed(self, rule: Recurrence | None) -> None:
        if not self._loading:
            self._save(lambda task: replace(task, recurrence=rule))

    @safe_callback
    def _on_estimate_apply(self, row: Adw.EntryRow) -> None:
        try:
            minutes = validation.parse_duration(row.get_text())
        except ValidationError as error:
            self._show_error(error)
            return
        self._save(lambda task: replace(task, estimate_min=minutes), "estimate")

    @safe_callback
    def _on_requested_by_apply(self, _row: Adw.EntryRow) -> None:
        self._save_requested_by()

    @safe_callback
    def _on_new_tag(self, _row: Adw.EntryRow) -> None:
        self._add_tag()

    @safe_callback
    def _on_remove_tag(self, _button: Gtk.Button, tag: str) -> None:
        if self._task_id is not None:
            remaining = [t for t in self._ctx.tasks.tags(self._task_id) if t != tag]
            self._ctx.tasks.set_tags(self._task_id, remaining)

    @safe_callback
    def _on_notes_changed(self, _buffer: Gtk.TextBuffer) -> None:
        if self._loading:
            return
        if self._notes_timeout is not None:
            GLib.source_remove(self._notes_timeout)
        self._notes_timeout = GLib.timeout_add(NOTES_SAVE_DELAY_MS, self._on_notes_timeout)

    @safe_callback
    def _on_notes_timeout(self) -> bool:
        self._notes_timeout = None
        self._save_notes()
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_notes_leave(self, _controller: Gtk.EventControllerFocus) -> None:
        self._flush_notes()

    @safe_callback
    def _on_new_item(self, row: Adw.EntryRow) -> None:
        if self._task_id is None or not row.get_text().strip():
            return
        try:
            self._ctx.tasks.add_checklist_item(self._task_id, row.get_text())
        except ValidationError as error:
            self._show_error(error)
            return
        row.set_text("")
        self._checklist_error.set_visible(False)

    @safe_callback
    def _on_item_toggled(self, check: Gtk.CheckButton, item_id: int) -> None:
        self._ctx.tasks.set_checklist_done(item_id, check.get_active())

    @safe_callback
    def _on_item_apply(self, row: Adw.EntryRow, item_id: int) -> None:
        try:
            self._ctx.tasks.rename_checklist_item(item_id, row.get_text())
        except ValidationError as error:
            self._show_error(error)

    @safe_callback
    def _on_item_remove(self, _button: Gtk.Button, item_id: int) -> None:
        self._ctx.tasks.delete_checklist_item(item_id)

    @safe_callback
    def _on_resume(self, _button: Gtk.Button) -> None:
        if self._task_id is not None:
            self._ctx.tasks.resume(self._task_id)

    @safe_callback
    def _on_hold_clicked(self, _button: Gtk.Button) -> None:
        self._open_hold_dialog()

    def _open_hold_dialog(self) -> None:
        if self._task_id is None:
            return
        dialog = HoldDialog(self._ctx, self._task_id)
        dialog.connect("closed", self._on_hold_dialog_closed)
        dialog.present(self)

    @safe_callback
    def _on_hold_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        # Cancelling puts the status drop-down back.
        self._reload()

    @safe_callback
    def _on_move_clicked(self, _button: Gtk.Button) -> None:
        if self._task_id is not None:
            MoveDialog(self._ctx, self._task_id).present(self)

    @safe_callback
    def _on_done_clicked(self, _button: Gtk.Button) -> None:
        if self._task is None:
            return
        if self._task.status == TaskStatus.DONE:
            self._ctx.tasks.reopen(self._task.id)
        else:
            self._ctx.tasks.complete(self._task.id)

    @safe_callback
    def _on_delete(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        if self._task is None:
            return
        task = self._task
        logged = self._ctx.tasks.logged_seconds(task.id)
        if not logged:
            self._ctx.tasks.delete(task.id)
            return
        alert = Adw.AlertDialog(
            heading=_("Delete “{title}”?").format(title=task.title),
            body=_(
                "It has {time} of tracked time, which is deleted with it. You can undo this "
                "for a few seconds."
            ).format(time=fmt_duration(logged // 60)),
        )
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("delete", _("_Delete"))
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        alert.connect("response", self._on_delete_response, task.id)
        alert.present(self)

    @safe_callback
    def _on_delete_response(self, _alert: Adw.AlertDialog, response: str, task_id: int) -> None:
        if response == "delete":
            self._ctx.tasks.delete(task_id)

    @safe_callback
    def _on_open_ticket(self, _button: Gtk.Button) -> None:
        if self._task is None:
            return
        url = self._ctx.projects.ticket_url(self._task)
        if not url:
            return
        root = self.get_root()
        window = root if isinstance(root, Gtk.Window) else None
        Gtk.UriLauncher.new(url).launch(window, None, self._on_launch_done)

    @safe_callback
    def _on_launch_done(self, launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self._ctx.bus.publish(Toast(_("Couldn't open the ticket link")))

    @safe_callback
    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._close()

    def _on_escape(self, _widget: Gtk.Widget, _args: GLib.Variant | None) -> bool:
        # Not @safe_callback: a shortcut callback must return a bool.
        try:
            self._close()
        except Exception as exc:
            report_error(exc)
        return True

    def _close(self) -> None:
        self._flush_notes()
        self._on_close()

    def _on_changed(self, event: Changed) -> None:
        if self._task_id is None:
            return
        mine = event.entity == Entity.TASK and (not event.ids or self._task_id in event.ids)
        related = event.entity in (Entity.PROJECT, Entity.PLAN, Entity.TIME_ENTRY, Entity.TIMER)
        if mine or related:
            self._reload()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self._reload()

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        # The window and the app flush before closing; this is the last chance otherwise.
        self._flush_notes()
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
