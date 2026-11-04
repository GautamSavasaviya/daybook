"""The sidebar: the seven sections, then "Projects & areas" with badge counts (D-051)."""

from collections.abc import Callable
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from daybook.core.models import Project, SidebarCounts
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.sections import sections
from daybook.ui.widgets.project_dot import DotPaintable


@dataclass(frozen=True)
class Destination:
    """Where a sidebar item leads: a fixed section (by key) or a project (by id)."""

    section: str | None = None
    project_id: int | None = None


_NEW_PROJECT = "new-project"
_ARCHIVED = "archived"
_BADGED = ("today", "inbox", "upcoming")


class Sidebar(Adw.Bin):
    def __init__(
        self,
        context: AppContext,
        *,
        on_navigate: Callable[[Destination, bool], None],
        on_new_project: Callable[[], None],
        on_show_archived: Callable[[], None],
    ) -> None:
        super().__init__()
        self._ctx = context
        self._on_navigate = on_navigate
        self._on_new_project = on_new_project
        self._on_show_archived = on_show_archived
        self._destinations: dict[Adw.SidebarItem, Destination | str] = {}
        self._badges: dict[str, Gtk.Label] = {}
        self._project_badges: dict[int, Gtk.Label] = {}
        self._project_items: dict[int, Adw.SidebarItem] = {}
        self._current = Destination(section=sections()[0].key)
        self._menu_project: int | None = None
        self._restoring = False
        self._counts_queued = False
        self._subscriptions: list[Subscription] = []

        self.widget = Adw.Sidebar()
        main = Adw.SidebarSection()
        for section in sections():
            item = Adw.SidebarItem(title=section.title, icon_name=section.icon)
            if section.key in _BADGED:
                badge = Gtk.Label(css_classes=["dim-label", "numeric"])
                item.set_suffix(badge)
                self._badges[section.key] = badge
            main.append(item)
            self._destinations[item] = Destination(section=section.key)
        self.widget.append(main)

        self._projects_section = Adw.SidebarSection(title=_("Projects & areas"))
        menu = Gio.Menu()
        menu.append(_("_Edit…"), "win.edit-context-project")
        menu.append(_("_Archive"), "win.archive-context-project")
        menu.append(_("_Delete…"), "win.delete-context-project")
        self._projects_section.set_menu_model(menu)
        self.widget.append(self._projects_section)

        manage = Adw.SidebarSection()
        new_item = Adw.SidebarItem(title=_("New Project or Area"), icon_name="list-add-symbolic")
        manage.append(new_item)
        self._destinations[new_item] = _NEW_PROJECT
        self._archived_item = Adw.SidebarItem(
            title=_("Archived Projects"), icon_name="folder-symbolic", visible=False
        )
        manage.append(self._archived_item)
        self._destinations[self._archived_item] = _ARCHIVED
        self.widget.append(manage)

        self.widget.connect("activated", self._on_activated)
        self.widget.connect("notify::selected", self._on_selected)
        self.widget.connect("setup-menu", self._on_setup_menu)
        self.set_child(self.widget)

        self.refresh_projects()
        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("destroy", self._on_destroy)
        self._refresh_counts()

    @property
    def menu_project(self) -> int | None:
        """The project whose context menu is open."""
        return self._menu_project

    def set_mode(self, mode: Adw.SidebarMode) -> None:
        self.widget.set_mode(mode)

    def select(self, destination: Destination) -> None:
        self._current = destination
        for item, target in self._destinations.items():
            if target == destination:
                self._restoring = True
                try:
                    self.widget.set_selected(item.get_index())
                finally:
                    self._restoring = False
                return

    def refresh_projects(self) -> None:
        # Removing the selected item moves the selection; that is not the user navigating.
        # If the current project is gone, the window decides where to go instead.
        self._restoring = True
        try:
            for item in self._project_items.values():
                self._destinations.pop(item, None)
            self._projects_section.remove_all()
            self._project_items.clear()
            self._project_badges.clear()
            for project in self._ctx.projects.all():
                self._add_project(project)
            self._archived_item.set_visible(bool(self._ctx.projects.archived()))
        finally:
            self._restoring = False
        self.select(self._current)
        self._refresh_counts()

    def _add_project(self, project: Project) -> None:
        kind = _("Area") if project.is_area else _("Project")
        item = Adw.SidebarItem(
            title=project.name,
            icon_paintable=DotPaintable(project.color),
            tooltip=f"{kind}: {project.name}",
        )
        badge = Gtk.Label(css_classes=["dim-label", "numeric"])
        item.set_suffix(badge)
        self._projects_section.append(item)
        self._destinations[item] = Destination(project_id=project.id)
        self._project_items[project.id] = item
        self._project_badges[project.id] = badge

    def _refresh_counts(self) -> None:
        # One write publishes several events (task, plan); count once per burst.
        if not self._counts_queued:
            self._counts_queued = True
            GLib.idle_add(self._load_counts)

    @safe_callback
    def _load_counts(self) -> bool:
        self._counts_queued = False
        self._ctx.tasks.load_counts(self._apply_counts)
        return GLib.SOURCE_REMOVE

    def _apply_counts(self, counts: SidebarCounts) -> None:
        self._badges["today"].set_text(str(counts.today) if counts.today else "")
        self._badges["inbox"].set_text(str(counts.inbox) if counts.inbox else "")
        self._badges["upcoming"].set_text(str(counts.upcoming) if counts.upcoming else "")
        for project_id, badge in self._project_badges.items():
            count = counts.per_project.get(project_id, 0)
            badge.set_text(str(count) if count else "")

    # Handlers ---------------------------------------------------------------------------

    def _target(self, index: int) -> Destination | str | None:
        item = self.widget.get_item(index)
        return self._destinations.get(item) if item is not None else None

    @safe_callback
    def _on_activated(self, _sidebar: Adw.Sidebar, index: int) -> None:
        target = self._target(index)
        if target == _NEW_PROJECT:
            self.select(self._current)
            self._on_new_project()
        elif target == _ARCHIVED:
            self.select(self._current)
            self._on_show_archived()
        elif isinstance(target, Destination):
            self._current = target
            self._on_navigate(target, True)

    @safe_callback
    def _on_selected(self, sidebar: Adw.Sidebar, _param: object) -> None:
        if self._restoring:
            return
        target = self._target(sidebar.get_selected())
        if isinstance(target, Destination) and target != self._current:
            self._current = target
            self._on_navigate(target, False)
        elif isinstance(target, str):
            # Action items never stay selected; their activation runs the action.
            GLib.idle_add(self._restore_selection)

    @safe_callback
    def _restore_selection(self) -> bool:
        self.select(self._current)
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_setup_menu(self, _sidebar: Adw.Sidebar, item: Adw.SidebarItem) -> None:
        target = self._destinations.get(item)
        self._menu_project = target.project_id if isinstance(target, Destination) else None

    def _on_changed(self, event: Changed) -> None:
        if event.entity == Entity.PROJECT:
            self.refresh_projects()
        elif event.entity in (Entity.TASK, Entity.PLAN, Entity.TIME_OFF):
            self._refresh_counts()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self._refresh_counts()

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
