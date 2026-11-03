"""The main window: sidebar, content, task detail panel, toasts and adaptive layout.

Wide (≥ 900 sp): sidebar | list | detail side by side. Narrower, the detail slides over the
list; below 600 sp the sidebar becomes its own page and the detail covers the whole
window (PLAN §6.3, SPEC A10).
"""

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from daybook.const import APP_ID, APP_NAME
from daybook.core.errors import NotFoundError, SchemaTooNewError
from daybook.core.models import GENERAL_PROJECT_ID
from daybook.core.tasklists import ListKind
from daybook.paths import Paths
from daybook.services.context import AppContext
from daybook.services.events import Changed, Entity, Subscription, Toast
from daybook.services.safe import safe_callback
from daybook.ui.dialogs.project_edit import ArchivedProjectsDialog, ProjectDialog
from daybook.ui.dialogs.task_new import NewTaskDialog
from daybook.ui.sections import Section, sections
from daybook.ui.sidebar import Destination, Sidebar
from daybook.ui.task_detail import TaskDetail
from daybook.ui.views.task_list import TaskListView

DETAIL_OVERLAY_BELOW = "max-width: 900sp"
COLLAPSE_BELOW = "max-width: 600sp"
_LIST_SECTIONS = {"inbox": ListKind.INBOX, "upcoming": ListKind.UPCOMING, "done": ListKind.DONE}


def _icon_button(icon: str, label: str) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, tooltip_text=label)
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return button


class MainWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        application: Adw.Application,
        paths: Paths,
        context: AppContext | None,
        startup_error: BaseException | None,
    ) -> None:
        super().__init__(application=application, title=APP_NAME)
        if application.get_application_id() != APP_ID:
            # A test run with DAYBOOK_* variables (D-065): make it impossible to mistake.
            self.add_css_class("devel")
            self.set_title(_("{name} (Devel)").format(name=APP_NAME))
        self.set_default_size(1180, 760)
        self.set_size_request(360, 360)
        self._paths = paths
        self._subscriptions: list[Subscription] = []

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)

        if context is None:
            self._toasts.set_child(self._build_error_page(startup_error))
            return
        self._ctx = context
        self._sections = {section.key: section for section in sections()}
        self._page_key = "today"
        self._return_to = "today"  # the page a finished search goes back to
        self._project_id: int | None = None
        self._toasts.set_child(self._build_main())
        self._add_actions()
        self._subscriptions.append(context.bus.subscribe(Toast, self._on_toast))
        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self.connect("destroy", self._on_destroy)
        self._navigate(Destination(section="today"), show_content=False)

    # Layout -------------------------------------------------------------------------

    def _build_main(self) -> Gtk.Widget:
        self._split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280)
        self._sidebar = Sidebar(
            self._ctx,
            on_navigate=self._navigate,
            on_new_project=self._new_project,
            on_show_archived=self._show_archived,
        )
        menu = Gio.Menu()
        menu.append(_("_About Daybook"), "app.about")
        menu.append(_("_Quit"), "app.quit")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            primary=True,
            tooltip_text=_("Main Menu"),
        )
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Main Menu")])
        sidebar_header = Adw.HeaderBar()
        sidebar_header.pack_end(menu_button)
        sidebar_view = Adw.ToolbarView(content=self._sidebar)
        sidebar_view.add_top_bar(sidebar_header)
        self._split.set_sidebar(
            Adw.NavigationPage(title=APP_NAME, child=sidebar_view, tag="sidebar")
        )

        self._detail = TaskDetail(self._ctx, on_close=self._close_detail)
        self._overlay = Adw.OverlaySplitView(
            sidebar_position=Gtk.PackType.END,
            show_sidebar=False,
            min_sidebar_width=340,
            max_sidebar_width=420,
            sidebar_width_fraction=0.4,
            # While the window narrows past a breakpoint the hidden panel can be measured
            # at width 0; a scroller with an external horizontal policy reports a minimum
            # width of 0 there, so GTK never sees a size below the panel's minimum.
            sidebar=Gtk.ScrolledWindow(
                child=self._detail,
                hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                vscrollbar_policy=Gtk.PolicyType.NEVER,
            ),
            content=self._build_content(),
        )
        self._overlay.connect("notify::show-sidebar", self._on_detail_shown)
        self._content_page = Adw.NavigationPage(title="", child=self._overlay, tag="content")
        self._split.set_content(self._content_page)

        overlay_detail = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(DETAIL_OVERLAY_BELOW))
        overlay_detail.add_setter(self._overlay, "collapsed", True)
        self.add_breakpoint(overlay_detail)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(COLLAPSE_BELOW))
        narrow.add_setter(self._split, "collapsed", True)
        narrow.add_setter(self._overlay, "collapsed", True)
        narrow.add_setter(self._overlay, "max-sidebar-width", 10_000.0)
        narrow.add_setter(self._overlay, "sidebar-width-fraction", 1.0)
        narrow.add_setter(self._sidebar.widget, "mode", Adw.SidebarMode.PAGE)
        self.add_breakpoint(narrow)
        return self._split

    def _build_content(self) -> Gtk.Widget:
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)
        self._lists: dict[str, TaskListView] = {}
        for section in self._sections.values():
            kind = _LIST_SECTIONS.get(section.key)
            if kind is None:
                self._stack.add_named(self._placeholder_page(section), section.key)
            else:
                view = self._list_view(kind)
                self._lists[section.key] = view
                self._stack.add_named(view, section.key)
        self._project_list = self._list_view(ListKind.PROJECT)
        self._stack.add_named(self._project_list, "project")
        self._search_list = self._list_view(ListKind.SEARCH)
        self._stack.add_named(self._search_list, "search")

        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle()
        header.set_title_widget(self._title)
        new_task = _icon_button("list-add-symbolic", _("New Task (Ctrl+N)"))
        new_task.set_action_name("win.new-task")
        header.pack_start(new_task)
        self._search_button = Gtk.ToggleButton(
            icon_name="system-search-symbolic", tooltip_text=_("Search (Ctrl+F)")
        )
        self._search_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Search")])
        header.pack_end(self._search_button)
        self._filter_button = Gtk.ToggleButton(
            icon_name="daybook-filter-symbolic", tooltip_text=_("Filter")
        )
        self._filter_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Filter")])
        self._filter_button.connect("toggled", self._on_filter_toggled)
        header.pack_end(self._filter_button)
        project_menu = Gio.Menu()
        project_menu.append(_("_Edit Project…"), "win.edit-project")
        project_menu.append(_("_Archive Project"), "win.archive-project")
        self._project_menu = Gtk.MenuButton(
            icon_name="view-more-symbolic", menu_model=project_menu, tooltip_text=_("Project")
        )
        self._project_menu.update_property([Gtk.AccessibleProperty.LABEL], [_("Project menu")])
        header.pack_end(self._project_menu)

        self._search_entry = Gtk.SearchEntry(
            placeholder_text=_("Search titles, tickets and notes"), hexpand=True
        )
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("stop-search", self._on_stop_search)
        self._search_bar = Gtk.SearchBar(
            child=Adw.Clamp(child=self._search_entry, maximum_size=600),
            show_close_button=False,
        )
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.bind_property(
            "search-mode-enabled",
            self._search_button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        self._search_bar.connect("notify::search-mode-enabled", self._on_search_mode)

        content = Adw.ToolbarView(content=self._stack)
        content.add_top_bar(header)
        content.add_top_bar(self._search_bar)
        return content

    def _list_view(self, kind: ListKind) -> TaskListView:
        if kind == ListKind.DONE:
            action = (_("Show Inbox"), lambda: self._navigate(Destination(section="inbox")))
        else:
            action = (_("New Task"), self._new_task)
        return TaskListView(
            self._ctx,
            kind,
            on_open=self._open_task,
            on_new_task=lambda title: self._new_task(title=title),
            empty_action=action,
        )

    def _placeholder_page(self, section: Section) -> Gtk.Widget:
        # Today, Plan, Meetings and Reports arrive in later phases (PLAN §7); until then
        # each explains what it will hold, with no dead buttons.
        return Adw.StatusPage(
            icon_name=section.icon,
            title=section.empty_title,
            description=section.empty_description,
        )

    def _build_error_page(self, error: BaseException | None) -> Gtk.Widget:
        if isinstance(error, SchemaTooNewError):
            description = _(
                "Your data was saved by a newer version of Daybook. Update Daybook to open "
                "it. Nothing was changed."
            )
        else:
            description = _(
                "Daybook could not open its database. Nothing was changed. The details "
                "are in the log."
            )
        button = Gtk.Button(label=_("Open Log Folder"), halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.connect("clicked", self._on_open_log_folder)
        page = Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title=_("Your Data Could Not Be Opened"),
            description=description,
            child=button,
        )
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page)
        return view

    # Actions ------------------------------------------------------------------------

    def _add_actions(self) -> None:
        for name, handler in (
            ("new-task", self._on_new_task_action),
            ("search", self._on_search_action),
            ("edit-project", self._on_edit_project),
            ("archive-project", self._on_archive_project),
            ("edit-context-project", self._on_edit_context_project),
            ("archive-context-project", self._on_archive_context_project),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _action(self, name: str) -> Gio.SimpleAction:
        action = self.lookup_action(name)
        assert isinstance(action, Gio.SimpleAction)
        return action

    # Navigation ---------------------------------------------------------------------

    def _navigate(self, destination: Destination, show_content: bool = True) -> None:
        self._search_bar.set_search_mode(False)
        if destination.project_id is not None:
            try:
                project = self._ctx.projects.get(destination.project_id)
            except NotFoundError:
                destination = Destination(section="inbox")
            else:
                self._project_id = project.id
                self._project_list.set_project(project.id, project.name)
                self._show_page("project", project.name, project.is_area)
        if destination.section is not None:
            self._project_id = None
            section = self._sections[destination.section]
            self._show_page(section.key, section.title, False)
        self._sidebar.select(destination)
        if show_content:
            self._split.set_show_content(True)

    def _show_page(self, key: str, title: str, is_area: bool) -> None:
        self._page_key = key
        self._stack.set_visible_child_name(key)
        self._title.set_title(title)
        self._title.set_subtitle(_("Area") if key == "project" and is_area else "")
        self._content_page.set_title(title)
        view = self._current_list()
        self._filter_button.set_visible(view is not None)
        self._filter_button.set_active(view is not None and view.filters.get_reveal_child())
        is_project = key == "project"
        self._project_menu.set_visible(is_project)
        general = self._project_id == GENERAL_PROJECT_ID
        self._action("archive-project").set_enabled(is_project and not general)
        self._action("edit-project").set_enabled(is_project)

    def _current_list(self) -> TaskListView | None:
        child = self._stack.get_visible_child()
        return child if isinstance(child, TaskListView) else None

    def _open_task(self, task_id: int) -> None:
        self._detail.show_task(task_id)
        self._overlay.set_show_sidebar(True)
        for view in (*self._lists.values(), self._project_list, self._search_list):
            if view is not self._current_list():
                view.set_selected_task(task_id)

    def _close_detail(self) -> None:
        self._overlay.set_show_sidebar(False)

    @safe_callback
    def _on_detail_shown(self, overlay: Adw.OverlaySplitView, _param: object) -> None:
        if not overlay.get_show_sidebar():
            for view in (*self._lists.values(), self._project_list, self._search_list):
                view.set_selected_task(None)
            current = self._current_list()
            if current is not None:
                current.grab_focus()

    def _new_task(self, *, title: str = "") -> None:
        project_id = self._project_id if self._page_key == "project" else None
        NewTaskDialog(self._ctx, project_id=project_id, title=title).present(self)

    def _new_project(self) -> None:
        ProjectDialog(
            self._ctx, on_saved=lambda pid: self._navigate(Destination(project_id=pid))
        ).present(self)

    def _show_archived(self) -> None:
        ArchivedProjectsDialog(self._ctx).present(self)

    def _edit_project(self, project_id: int) -> None:
        ProjectDialog(self._ctx, project_id, on_removed=self._on_project_removed).present(self)

    def _archive_project(self, project_id: int) -> None:
        if project_id == GENERAL_PROJECT_ID:
            return
        self._ctx.projects.archive(project_id)
        self._on_project_removed(project_id)

    def _on_project_removed(self, project_id: int) -> None:
        if self._page_key == "project" and self._project_id == project_id:
            self._navigate(Destination(section="inbox"), show_content=False)

    # Handlers -----------------------------------------------------------------------

    @safe_callback
    def _on_new_task_action(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        self._new_task()

    @safe_callback
    def _on_search_action(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        self._search_bar.set_search_mode(True)
        self._search_entry.grab_focus()

    @safe_callback
    def _on_edit_project(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        if self._project_id is not None:
            self._edit_project(self._project_id)

    @safe_callback
    def _on_archive_project(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        if self._project_id is not None:
            self._archive_project(self._project_id)

    @safe_callback
    def _on_edit_context_project(
        self, _action: Gio.SimpleAction, _param: GLib.Variant | None
    ) -> None:
        if self._sidebar.menu_project is not None:
            self._edit_project(self._sidebar.menu_project)

    @safe_callback
    def _on_archive_context_project(
        self, _action: Gio.SimpleAction, _param: GLib.Variant | None
    ) -> None:
        if self._sidebar.menu_project is not None:
            self._archive_project(self._sidebar.menu_project)

    @safe_callback
    def _on_filter_toggled(self, button: Gtk.ToggleButton) -> None:
        view = self._current_list()
        if view is not None:
            view.filters.set_reveal_child(button.get_active())

    @safe_callback
    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        text = entry.get_text()
        if text.strip():
            self._search_list.set_text(text)
            if self._page_key != "search":
                self._return_to = self._page_key
            self._show_page("search", _("Search"), False)
        elif self._page_key == "search":
            self._leave_search()

    @safe_callback
    def _on_stop_search(self, _entry: Gtk.SearchEntry) -> None:
        self._search_bar.set_search_mode(False)

    @safe_callback
    def _on_search_mode(self, bar: Gtk.SearchBar, _param: object) -> None:
        if bar.get_search_mode():
            self._search_entry.grab_focus()
            return
        self._search_entry.set_text("")
        if self._page_key == "search":
            self._leave_search()

    def _leave_search(self) -> None:
        previous = self._return_to
        if previous == "project" and self._project_id is not None:
            project = self._ctx.projects.get(self._project_id)
            self._show_page("project", project.name, project.is_area)
        else:
            self._show_page(previous, self._sections[previous].title, False)

    def _on_changed(self, event: Changed) -> None:
        if event.entity != Entity.PROJECT or self._page_key != "project":
            return
        if self._project_id is None:
            return
        try:
            project = self._ctx.projects.get(self._project_id)
        except NotFoundError:
            self._navigate(Destination(section="inbox"), show_content=False)
            return
        if project.archived:
            self._navigate(Destination(section="inbox"), show_content=False)
        else:
            self._project_list.set_project(project.id, project.name)
            self._show_page("project", project.name, project.is_area)

    def _on_toast(self, event: Toast) -> None:
        # Deliberately not @safe_callback: reporting a failure here would publish another
        # Toast and loop. The EventBus logs exceptions from Toast handlers instead.
        # Messages are plain text (task titles may contain "&" or "<"), never markup.
        toast = Adw.Toast(title=event.message, use_markup=False)
        if event.undo is not None:
            undo = event.undo
            toast.set_button_label(_("_Undo"))

            def on_undo(_toast: Adw.Toast) -> None:
                undo()

            toast.connect("button-clicked", safe_callback(on_undo))
        self._toasts.add_toast(toast)

    @safe_callback
    def _on_open_log_folder(self, _button: Gtk.Button) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self._paths.log_dir)))
        launcher.launch(self, None, None)

    @safe_callback
    def _on_destroy(self, _window: Gtk.Window) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
