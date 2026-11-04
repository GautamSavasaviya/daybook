"""The main window: sidebar, content, task detail panel, toasts and adaptive layout.

Wide (≥ 900 sp): sidebar | list | detail side by side. Narrower, the detail slides over the
list; below 600 sp the sidebar becomes its own page and the detail covers the whole
window (PLAN §6.3, SPEC A10).
"""

from datetime import timedelta
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from daybook.const import APP_ID, APP_NAME
from daybook.core.errors import NotFoundError, SchemaTooNewError
from daybook.core.models import GENERAL_PROJECT_ID, Occurrence, TaskStatus
from daybook.core.reminders import View
from daybook.core.tasklists import ListKind
from daybook.core.timeutil import fmt_duration
from daybook.paths import Paths
from daybook.services.context import AppContext
from daybook.services.events import (
    Changed,
    Entity,
    ReminderGone,
    ReminderShown,
    Subscription,
    Toast,
)
from daybook.services.safe import report_error, safe_callback
from daybook.services.text import short
from daybook.ui.dialogs.meeting_edit import MeetingDialog, MeetingNowDialog, ask_scope
from daybook.ui.dialogs.project_edit import ArchivedProjectsDialog, ProjectDialog
from daybook.ui.dialogs.quick_add import QuickAddDialog
from daybook.ui.dialogs.task_new import NewTaskDialog
from daybook.ui.dialogs.time_off import TimeOffDialog
from daybook.ui.focus_window import FocusWindow
from daybook.ui.sections import sections
from daybook.ui.shortcuts import shortcuts_dialog
from daybook.ui.sidebar import Destination, Sidebar
from daybook.ui.task_detail import TaskDetail
from daybook.ui.timer_actions import start_timer, toggle_timer
from daybook.ui.views.meetings import MeetingsView
from daybook.ui.views.plan import PlanView
from daybook.ui.views.reports import ReportsView
from daybook.ui.views.review import ReviewView
from daybook.ui.views.task_list import TaskListView
from daybook.ui.views.today import TodayView
from daybook.ui.widgets.reminder_card import ReminderCard
from daybook.ui.widgets.task_row import TaskRow

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
        self._add_section_action()
        self._add_space_key()
        self._focus_window: FocusWindow | None = None
        self._subscriptions.append(context.bus.subscribe(Toast, self._on_toast))
        self._subscriptions.append(context.bus.subscribe(ReminderShown, self._on_reminder))
        self._subscriptions.append(context.bus.subscribe(ReminderGone, self._on_reminder_gone))
        self._card.show_most_urgent(context.notifier.live)
        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self.connect("destroy", self._on_destroy)
        self.connect("close-request", self._on_close_request)
        self._navigate(Destination(section="today"), show_content=False)
        self._update_timer_chip()
        minute = context.clock.real_seconds_until(context.clock.now() + timedelta(minutes=1))
        self._chip_tick: int | None = GLib.timeout_add_seconds(
            max(1, min(30, int(minute))), self._on_chip_tick
        )

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
        menu.append(_("_Quick Add…"), "win.quick-add")
        menu.append(_("_Focus Window"), "win.focus")
        menu.append(_("_Time Off…"), "win.time-off")
        menu.append(_("_Preferences"), "app.preferences")
        menu.append(_("_Keyboard Shortcuts"), "win.shortcuts")
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
        narrow.add_setter(self._chip_bin, "visible", False)
        self.add_breakpoint(narrow)
        return self._split

    def _build_content(self) -> Gtk.Widget:
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)
        self._lists: dict[str, TaskListView] = {}
        self._today = TodayView(
            self._ctx,
            on_open_task=self._open_task,
            on_plan=lambda: self._navigate(Destination(section="plan")),
            on_meeting_now=self._meeting_now,
            on_open_meeting=self._open_occurrence,
            on_focus=self._show_focus,
            on_review=lambda: self.show_view(View.REVIEW),
        )
        self._review = ReviewView(
            self._ctx,
            on_open_task=self._open_task,
            on_plan_next=self._plan_next_day,
            on_finish=lambda: self._navigate(Destination(section="today")),
        )
        self._plan = PlanView(
            self._ctx,
            on_open_task=self._open_task,
            on_start_day=self._start_day,
            on_show_inbox=lambda: self._navigate(Destination(section="inbox")),
        )
        self._meetings = MeetingsView(
            self._ctx,
            on_new=self._new_meeting,
            on_meeting_now=self._meeting_now,
            on_open_occurrence=self._open_occurrence,
            on_open_series=self._open_series,
        )
        pages: dict[str, Gtk.Widget] = {
            "today": self._today,
            "plan": self._plan,
            "meetings": self._meetings,
            "reports": ReportsView(self._ctx),
        }
        for section in self._sections.values():
            if section.key in pages:
                self._stack.add_named(pages[section.key], section.key)
            else:
                view = self._list_view(_LIST_SECTIONS[section.key])
                self._lists[section.key] = view
                self._stack.add_named(view, section.key)
        self._project_list = self._list_view(ListKind.PROJECT)
        self._stack.add_named(self._project_list, "project")
        self._search_list = self._list_view(ListKind.SEARCH)
        self._stack.add_named(self._search_list, "search")
        self._stack.add_named(self._review, "review")

        header = self._build_header()

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

        self._card = ReminderCard(self._on_card_action)
        self._card.add_css_class("main-reminder")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(Adw.Clamp(child=self._card, maximum_size=760))
        body.append(self._stack)
        self._stack.set_vexpand(True)
        content = Adw.ToolbarView(content=body)
        content.add_top_bar(header)
        content.add_top_bar(self._search_bar)
        return content

    def _build_header(self) -> Adw.HeaderBar:
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
        # Always shows what is being timed, from any page; opens the Focus window.
        self._timer_chip = Gtk.Button(css_classes=["flat"], visible=False)
        self._timer_chip.set_action_name("win.focus")
        # Hidden on narrow windows, where it would squeeze the title; the Now card shows it.
        self._chip_bin = Adw.Bin(child=self._timer_chip)
        header.pack_end(self._chip_bin)
        return header

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
            ("meeting-now", self._on_meeting_now_action),
            ("new-meeting", self._on_new_meeting_action),
            ("time-off", self._on_time_off_action),
            ("search", self._on_search_action),
            ("focus", self._on_focus_action),
            ("done-selected", self._on_done_selected),
            ("quick-add", self._on_quick_add_action),
            ("shortcuts", self._on_shortcuts_action),
            ("edit-project", self._on_edit_project),
            ("archive-project", self._on_archive_project),
            ("edit-context-project", self._on_edit_context_project),
            ("archive-context-project", self._on_archive_context_project),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _add_section_action(self) -> None:
        # win.section::today … win.section::done, bound to Ctrl+1…7 (D-051).
        action = Gio.SimpleAction.new("section", GLib.VariantType("s"))
        action.connect("activate", self._on_section_action)
        self.add_action(action)

    def _add_space_key(self) -> None:
        # Capture phase: list rows would otherwise take Space to select or activate.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

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

    def show_view(self, view: View) -> None:
        """Where a notification click leads (PLAN §5.5)."""
        if view == View.REVIEW:
            self._search_bar.set_search_mode(False)
            self._sidebar.select(Destination(section="today"))
            self._show_page("review", _("Review"), False)
            self._split.set_show_content(True)
            return
        self._navigate(Destination(section=view.value))
        if view == View.PLAN:
            self._plan.show_today()

    def _plan_next_day(self) -> None:
        self._navigate(Destination(section="plan"))
        self._plan.show_next_day()

    def _show_live_reminders(self) -> None:
        live = self._ctx.notifier.live
        self._card.show_most_urgent(live)
        if self._focus_window is not None:
            self._focus_window.show_reminders(live)

    def _on_reminder(self, _event: ReminderShown) -> None:
        self._show_live_reminders()

    def _on_reminder_gone(self, _event: ReminderGone) -> None:
        self._show_live_reminders()

    def _on_card_action(self, key: str, action: str) -> None:
        self._ctx.reminders.handle(key, action)

    def _show_focus(self) -> None:
        if self._focus_window is None:
            application = self.get_application()
            assert isinstance(application, Adw.Application)
            self._focus_window = FocusWindow(application, self._ctx, self._on_card_action)
            self._focus_window.connect("close-request", self._on_focus_closed)
            self._focus_window.show_reminders(self._ctx.notifier.live)
        self._focus_window.present()

    def _start_day(self) -> None:
        """Plan → Start Day: go to Today and time the first planned item (D-084)."""
        self._navigate(Destination(section="today"))
        if self._ctx.timer.running_task() is not None:
            return
        first = self._ctx.plan.first_open(self._ctx.tasks.today())
        if first is not None:
            start_timer(self, self._ctx, first.summary.task.id)

    def _focused_task(self, focus: Gtk.Widget | None) -> int | None:
        """The task of the focused row in any list, the timeline or the plan."""
        if focus is None:
            return None
        for widget in (focus, focus.get_first_child(), focus.get_ancestor(TaskRow)):
            if isinstance(widget, TaskRow) and widget.task_id is not None:
                return widget.task_id
        return self._today.task_for_row(focus) or self._plan.task_for_row(focus)

    def _selected_task(self) -> int | None:
        """What Space and Ctrl+Enter act on without a focused row: the open task, the Now
        card's task on Today, or the timed task."""
        if self._overlay.get_show_sidebar() and self._detail.task_id is not None:
            return self._detail.task_id
        if self._page_key == "today" and self._today.current_task is not None:
            return self._today.current_task
        state = self._ctx.timer.state()
        return state.task_id if state is not None else None

    def quick_add(self, text: str = "") -> None:
        """Ctrl+K and `daybook --quick-add [text]`. Another open dialog doesn't block it:
        dialogs stack, and closing this one returns to the other (D-108)."""
        if not isinstance(self.get_visible_dialog(), QuickAddDialog):
            QuickAddDialog(self._ctx, text).present(self)

    def meeting_now(self) -> None:
        """Ctrl+Shift+M and `daybook --meeting-now`: an emergency can't wait for another
        dialog to be closed."""
        if not isinstance(self.get_visible_dialog(), MeetingNowDialog):
            self._meeting_now()

    def _new_task(self, *, title: str = "") -> None:
        project_id = self._project_id if self._page_key == "project" else None
        NewTaskDialog(self._ctx, project_id=project_id, title=title).present(self)

    def _meeting_now(self) -> None:
        MeetingNowDialog(self._ctx).present(self)

    def _new_meeting(self) -> None:
        MeetingDialog(self._ctx).present(self)

    def _open_series(self, meeting_id: int) -> None:
        MeetingDialog(self._ctx, meeting_id=meeting_id).present(self)

    def _open_occurrence(self, occurrence: Occurrence) -> None:
        """Recurring meetings ask first: this occurrence or all of them."""
        if not occurrence.recurring:
            self._open_series(occurrence.meeting_id)
            return

        def chosen(all_occurrences: bool) -> None:
            if all_occurrences:
                self._open_series(occurrence.meeting_id)
            else:
                MeetingDialog(self._ctx, occurrence=occurrence).present(self)

        ask_scope(self, occurrence.title, chosen)

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
    def _on_meeting_now_action(
        self, _action: Gio.SimpleAction, _param: GLib.Variant | None
    ) -> None:
        self.meeting_now()

    @safe_callback
    def _on_new_meeting_action(
        self, _action: Gio.SimpleAction, _param: GLib.Variant | None
    ) -> None:
        self._new_meeting()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        # Not @safe_callback: the handler must return a bool.
        modifiers = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
        modifiers |= Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.SUPER_MASK
        if keyval != Gdk.KEY_space or state & modifiers or self.get_visible_dialog() is not None:
            return False
        try:
            focus = self.get_focus()
            # Typing, buttons, switches and the like keep Space for themselves.
            task_id = self._selected_task() if focus is None else self._focused_task(focus)
            if task_id is None:
                return False
            toggle_timer(self, self._ctx, task_id)
        except Exception as exc:
            report_error(exc)
        return True

    @safe_callback
    def _on_quick_add_action(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        self.quick_add()

    @safe_callback
    def _on_shortcuts_action(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        shortcuts_dialog().present(self)

    @safe_callback
    def _on_section_action(self, _action: Gio.SimpleAction, param: GLib.Variant | None) -> None:
        if param is None or self.get_visible_dialog() is not None:
            return
        key = param.get_string()
        if key in self._sections:
            self._navigate(Destination(section=key))

    @safe_callback
    def _on_focus_action(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        self._show_focus()

    @safe_callback
    def _on_done_selected(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        if self.get_visible_dialog() is not None:
            return
        task_id = self._focused_task(self.get_focus()) or self._selected_task()
        if task_id is not None and self._ctx.tasks.get(task_id).status != TaskStatus.DONE:
            self._ctx.tasks.complete(task_id)

    @safe_callback
    def _on_focus_closed(self, _window: Gtk.Window) -> bool:
        self._focus_window = None
        return False

    @safe_callback
    def _on_time_off_action(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        TimeOffDialog(self._ctx).present(self)

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
        if previous == "review":
            self.show_view(View.REVIEW)
            return
        if previous != "project":
            self._show_page(previous, self._sections[previous].title, False)
            return
        try:
            project = (
                self._ctx.projects.get(self._project_id) if self._project_id is not None else None
            )
        except NotFoundError:
            project = None
        if project is None or project.archived:
            # Deleted or archived while the search was open (D-070): it is no longer shown.
            self._navigate(Destination(section="inbox"), show_content=False)
        else:
            self._show_page("project", project.name, project.is_area)

    def _update_timer_chip(self) -> None:
        state = self._ctx.timer.state()
        if state is None:
            self._timer_chip.set_visible(False)
            return
        try:
            task = self._ctx.tasks.get(state.task_id)
        except NotFoundError:
            self._timer_chip.set_visible(False)
            return
        name = task.ticket_key or short(task.title, 18)
        minutes = fmt_duration(self._ctx.timer.tracked_today_s(task.id) // 60)
        icon = "media-record-symbolic" if state.running else "media-playback-pause-symbolic"
        # can-shrink: on a narrow window the label ellipsizes instead of widening the header.
        self._timer_chip.set_child(
            Adw.ButtonContent(icon_name=icon, label=f"{name} · {minutes}", can_shrink=True)
        )
        verb = _("Timing") if state.running else _("Paused")
        tip = _("{verb} {task}: {time} today. Open the Focus window").format(
            verb=verb, task=name, time=minutes
        )
        self._timer_chip.set_tooltip_text(tip)
        self._timer_chip.update_property([Gtk.AccessibleProperty.LABEL], [tip])
        self._timer_chip.set_visible(True)

    def _on_chip_tick(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the ticking for good.
        try:
            self._update_timer_chip()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_CONTINUE

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.TIMER, Entity.TIME_ENTRY, Entity.TASK):
            self._update_timer_chip()
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

    def flush_pending(self) -> None:
        """Save edits still waiting for a delayed save; called before the database closes."""
        if hasattr(self, "_detail"):
            self._detail.flush()

    @safe_callback
    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.flush_pending()
        return False  # let the window close

    @safe_callback
    def _on_destroy(self, _window: Gtk.Window) -> None:
        tick = getattr(self, "_chip_tick", None)
        if tick is not None:
            GLib.source_remove(tick)
            self._chip_tick = None
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
