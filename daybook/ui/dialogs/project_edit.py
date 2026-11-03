"""Create or edit a project or area (D-018, D-019, D-043).

A project has a ticket prefix and/or a ticket link; an area (Docs, Demos…) has neither.
General can only be renamed and recoloured.
"""

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core.models import ProjectColor, ProjectDraft
from daybook.services.context import AppContext
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.widgets.project_dot import color_name


class ProjectDialog(FormDialog):
    def __init__(
        self,
        context: AppContext,
        project_id: int | None = None,
        *,
        on_saved: Callable[[int], None] | None = None,
        on_removed: Callable[[int], None] | None = None,
    ) -> None:
        editing = project_id is not None
        super().__init__(
            title=_("Edit Project") if editing else _("New Project or Area"),
            primary_label=_("_Save") if editing else _("_Create"),
        )
        self._ctx = context
        self._project_id = project_id
        self._on_saved = on_saved
        self._on_removed = on_removed
        project = context.projects.get(project_id) if project_id is not None else None

        main = Adw.PreferencesGroup()
        self._name = Adw.EntryRow(
            title=_("Name"), text=project.name if project else "", activates_default=True
        )
        main.add(self._name)
        self.register_field("name", self._name)

        color_row = Adw.ActionRow(title=_("Colour"))
        swatches = Adw.WrapBox(child_spacing=8, line_spacing=8, margin_top=6, margin_bottom=6)
        self._swatches: dict[ProjectColor, Gtk.ToggleButton] = {}
        first: Gtk.ToggleButton | None = None
        current = project.color if project else ProjectColor.BLUE
        for color in ProjectColor:
            swatch = Gtk.ToggleButton(
                group=first,
                css_classes=["color-swatch", color.value],
                tooltip_text=color_name(color),
                valign=Gtk.Align.CENTER,
            )
            swatch.set_child(Gtk.Image(icon_name="object-select-symbolic", visible=False))
            swatch.update_property([Gtk.AccessibleProperty.LABEL], [color_name(color)])
            swatch.connect("toggled", self._on_swatch_toggled)
            first = first or swatch
            swatches.append(swatch)
            self._swatches[color] = swatch
        self._swatches[current].set_active(True)
        color_row.add_suffix(swatches)
        main.add(color_row)
        self.page.add(main)

        self._prefix = Adw.EntryRow(
            title=_("Ticket prefix, e.g. PROJ"),
            text=(project.key_prefix or "") if project else "",
        )
        self._url = Adw.EntryRow(
            title=_("Ticket link, e.g. https://pms.example.com/browse/{key}"),
            text=(project.url_template or "") if project else "",
        )
        self.register_field("key_prefix", self._prefix)
        self.register_field("url_template", self._url)
        if project is None or not project.is_general:
            tickets = Adw.PreferencesGroup(
                title=_("Tickets"),
                description=_(
                    "Leave both empty for an area such as Docs or Demos. With a prefix, "
                    "tickets like PROJ-123 are filed here automatically."
                ),
            )
            tickets.add(self._prefix)
            tickets.add(self._url)
            self.page.add(tickets)

        if project is not None and not project.is_general:
            self.page.add(self._danger_group(project.id))

    def _danger_group(self, project_id: int) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        if self._ctx.projects.can_delete(project_id):
            delete = Adw.ButtonRow(title=_("Delete Project"), css_classes=["destructive-action"])
            delete.connect("activated", self._on_delete)
            group.add(delete)
        else:
            open_count = self._ctx.projects.open_task_count(project_id)
            group.set_description(
                _(
                    "Archiving hides it and its {count} open tasks from your lists. Done "
                    "tasks stay in Done and search. You can restore it any time."
                ).format(count=open_count)
            )
        archive = Adw.ButtonRow(title=_("Archive Project"))
        archive.connect("activated", self._on_archive)
        group.add(archive)
        return group

    def _color(self) -> ProjectColor:
        for color, swatch in self._swatches.items():
            if swatch.get_active():
                return color
        return ProjectColor.BLUE

    @safe_callback
    def _on_swatch_toggled(self, swatch: Gtk.ToggleButton) -> None:
        mark = swatch.get_child()
        if mark is not None:
            mark.set_visible(swatch.get_active())

    def submit(self) -> None:
        draft = ProjectDraft(
            name=self._name.get_text(),
            color=self._color(),
            key_prefix=self._prefix.get_text(),
            url_template=self._url.get_text(),
        )
        if self._project_id is None:
            project_id = self._ctx.projects.create(draft)
        else:
            project_id = self._project_id
            self._ctx.projects.update(project_id, draft)
        if self._on_saved is not None:
            self._on_saved(project_id)

    @safe_callback
    def _on_archive(self, _row: Adw.ButtonRow) -> None:
        assert self._project_id is not None
        project_id = self._project_id
        self.close()
        self._ctx.projects.archive(project_id)
        if self._on_removed is not None:
            self._on_removed(project_id)

    @safe_callback
    def _on_delete(self, _row: Adw.ButtonRow) -> None:
        assert self._project_id is not None
        project = self._ctx.projects.get(self._project_id)
        alert = Adw.AlertDialog(
            heading=_("Delete “{name}”?").format(name=short(project.name)),
            body=_("It has no tasks. This can't be undone."),
        )
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("delete", _("_Delete"))
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        alert.connect("response", self._on_delete_response)
        alert.present(self)

    @safe_callback
    def _on_delete_response(self, _alert: Adw.AlertDialog, response: str) -> None:
        if response != "delete" or self._project_id is None:
            return
        project_id = self._project_id
        self._ctx.projects.delete(project_id)
        self.close()
        if self._on_removed is not None:
            self._on_removed(project_id)


class ArchivedProjectsDialog(Adw.Dialog):
    """Archived projects, each with Restore."""

    def __init__(self, context: AppContext) -> None:
        super().__init__(title=_("Archived Projects"), content_width=420, content_height=420)
        self._ctx = context
        self._stack = Gtk.Stack()
        self._group = Adw.PreferencesGroup()
        page = Adw.PreferencesPage()
        page.add(self._group)
        self._stack.add_named(page, "list")
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="folder-symbolic",
                title=_("No Archived Projects"),
                description=_("Projects you archive appear here, ready to restore."),
            ),
            "empty",
        )
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self._stack)
        self.set_child(view)
        self._rows: list[Gtk.Widget] = []
        self._fill()

    def _fill(self) -> None:
        for row in self._rows:
            self._group.remove(row)
        self._rows.clear()
        archived = self._ctx.projects.archived()
        for project in archived:
            row = Adw.ActionRow(title=project.name, use_markup=False)
            restore = Gtk.Button(label=_("Restore"), valign=Gtk.Align.CENTER)
            restore.connect("clicked", self._on_restore, project.id)
            row.add_suffix(restore)
            self._group.add(row)
            self._rows.append(row)
        self._stack.set_visible_child_name("list" if archived else "empty")

    @safe_callback
    def _on_restore(self, _button: Gtk.Button, project_id: int) -> None:
        self._ctx.projects.unarchive(project_id)
        self._fill()
