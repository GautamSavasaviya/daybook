"""New task (Ctrl+N): only the title is required; everything else is optional (SPEC A1).

Visible: Title, Project or area, Priority and "Plan for today" with a time. The rest sits
under a collapsed "More details" (D-057).
"""

from datetime import date
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.core.models import (
    DEFAULT_PRIORITY,
    GENERAL_PROJECT_ID,
    PlanRequest,
    Priority,
    TaskDraft,
)
from daybook.core.timeutil import fmt_day
from daybook.services.context import AppContext
from daybook.services.events import Toast
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.widgets.date_button import DateButton
from daybook.ui.widgets.suggest import Suggestions

PRESETS: tuple[tuple[str, int | None], ...] = (
    ("15m", 15),
    ("30m", 30),
    ("1h", 60),
    ("2h", 120),
    ("day", None),
)
DEFAULT_PRESET = "30m"


def priority_toggles(active: Priority) -> Adw.ToggleGroup:
    group = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
    for priority in Priority:
        group.add(
            Adw.Toggle(
                name=str(priority.value),
                label=f"P{priority.value}",
                tooltip=_("Priority {n}").format(n=priority.value),
            )
        )
    group.set_active_name(str(active.value))
    return group


def allotment_toggles() -> Adw.ToggleGroup:
    group = Adw.ToggleGroup(valign=Gtk.Align.CENTER, can_shrink=True)
    for name, minutes in PRESETS:
        label = _("Rest of day") if minutes is None else name
        group.add(Adw.Toggle(name=name, label=label))
    group.set_active_name(DEFAULT_PRESET)
    return group


class NewTaskDialog(FormDialog):
    def __init__(
        self, context: AppContext, *, project_id: int | None = None, title: str = ""
    ) -> None:
        super().__init__(title=_("New Task"), primary_label=_("_Add"), height=600)
        self._ctx = context
        self._projects = context.projects.all()
        self._project_chosen = project_id is not None
        self._inferring = False
        today = context.tasks.today()
        calendar = context.calendar.calendar()
        self._plan_day = calendar.working_on_or_after(today)
        self.page.add(self._build_main(project_id, title, today))
        self.page.add(self._build_details())
        self.set_focus(self._title)

    def _build_main(self, project_id: int | None, title: str, today: date) -> Adw.PreferencesGroup:
        main = Adw.PreferencesGroup()
        self._title = Adw.EntryRow(title=_("Title"), text=title, activates_default=True)
        main.add(self._title)
        self.register_field("title", self._title)

        names = Gtk.StringList.new([p.name for p in self._projects])
        self._project = Adw.ComboRow(title=_("Project or area"), model=names, enable_search=True)
        self._select_project(project_id or GENERAL_PROJECT_ID)
        self._project.connect("notify::selected", self._on_project_picked)
        main.add(self._project)
        self.register_field("project", self._project)

        priority_row = Adw.ActionRow(title=_("Priority"))
        self._priority = priority_toggles(DEFAULT_PRIORITY)
        priority_row.add_suffix(self._priority)
        main.add(priority_row)

        when = _("today") if self._plan_day == today else fmt_day(self._plan_day, today)
        self._plan = Adw.ExpanderRow(
            title=_("Plan for {day}").format(day=when),
            subtitle=_("Give it time in that day's plan"),
            show_enable_switch=True,
            enable_expansion=False,
        )
        time_row = Adw.ActionRow(title=_("Time"))
        self._allotment = allotment_toggles()
        time_row.add_suffix(self._allotment)
        self._plan.add_row(time_row)
        self._custom_time = Adw.EntryRow(title=_("Other time, e.g. 45m or 1h 30m"))
        self._plan.add_row(self._custom_time)
        main.add(self._plan)
        self.register_field("allotment", self._custom_time)
        self.register_field("plan", self._plan)
        return main

    def _build_details(self) -> Adw.PreferencesGroup:
        context = self._ctx
        details = Adw.PreferencesGroup()
        self._more = Adw.ExpanderRow(title=_("More details"))
        self._ticket = Adw.EntryRow(title=_("Ticket key, e.g. PROJ-123"))
        self._ticket.connect("changed", self._on_ticket_changed)
        self._ticket_hint = Adw.ActionRow(visible=False, css_classes=["dim-label"])
        self._due = DateButton(
            today=context.tasks.today, empty_label=_("None"), accessible_name=_("Due date")
        )
        due_row = Adw.ActionRow(title=_("Due date"))
        due_row.add_suffix(self._due)
        self._estimate = Adw.EntryRow(title=_("Estimate, e.g. 2h"))
        self._requested_by = Adw.EntryRow(title=_("Requested by"))
        Suggestions(self._requested_by, context.tasks.requested_by_suggestions)
        self._tags = Adw.EntryRow(title=_("Tags, separated by spaces"))
        self._notes = Adw.EntryRow(title=_("Notes"))
        for row in (
            self._ticket,
            self._ticket_hint,
            due_row,
            self._estimate,
            self._requested_by,
            self._tags,
            self._notes,
        ):
            self._more.add_row(row)
        details.add(self._more)
        for field, row in (
            ("ticket_key", self._ticket),
            ("estimate", self._estimate),
            ("requested_by", self._requested_by),
            ("tags", self._tags),
            ("notes", self._notes),
        ):
            self.register_field(field, row)
        return details

    def show_error(self, error: ValidationError) -> None:
        # Reveal the collapsed part that holds the field with the error.
        if error.field in ("ticket_key", "estimate", "requested_by", "tags", "notes"):
            self._more.set_expanded(True)
        if error.field == "allotment":
            self._plan.set_expanded(True)
        super().show_error(error)

    def _select_project(self, project_id: int) -> None:
        for index, project in enumerate(self._projects):
            if project.id == project_id:
                self._project.set_selected(index)
                return

    def _selected_project_id(self) -> int:
        index = self._project.get_selected()
        if index < len(self._projects):
            return self._projects[index].id
        return GENERAL_PROJECT_ID

    @safe_callback
    def _on_project_picked(self, _row: Adw.ComboRow, _param: object) -> None:
        if not self._inferring:
            self._project_chosen = True

    @safe_callback
    def _on_ticket_changed(self, row: Adw.EntryRow) -> None:
        key = row.get_text().strip()
        hints: list[str] = []
        if key:
            project = self._ctx.projects.project_for_key(key)
            if project is not None and not self._project_chosen:
                self._inferring = True
                try:
                    self._select_project(project.id)
                finally:
                    self._inferring = False
                hints.append(_("Filed under {project}").format(project=project.name))
            normalised = validation.ticket_key(key) if " " not in key else None
            if normalised and self._ctx.tasks.ticket_key_in_use(normalised):
                hints.append(_("Another task already has this ticket"))
        self._ticket_hint.set_title(" · ".join(hints))
        self._ticket_hint.set_visible(bool(hints))

    def _allotment_minutes(self) -> int | None:
        custom = validation.parse_duration(self._custom_time.get_text(), "allotment")
        if custom is not None:
            return validation.allotment(custom)
        name = self._allotment.get_active_name()
        return dict(PRESETS).get(name or DEFAULT_PRESET, 30)

    def submit(self) -> None:
        plan = None
        if self._plan.get_enable_expansion():
            plan = PlanRequest(self._plan_day, self._allotment_minutes())
        draft = TaskDraft(
            title=self._title.get_text(),
            project_id=self._selected_project_id(),
            priority=Priority(int(self._priority.get_active_name() or DEFAULT_PRIORITY)),
            ticket_key=self._ticket.get_text(),
            requested_by=self._requested_by.get_text(),
            estimate_min=validation.parse_duration(self._estimate.get_text()),
            due_date=self._due.value,
            notes=self._notes.get_text(),
            tags=validation.split_tags(self._tags.get_text()),
            plan=plan,
        )
        self._ctx.tasks.create(draft)
        self._ctx.bus.publish(Toast(_("Added “{title}”").format(title=short(draft.title))))
