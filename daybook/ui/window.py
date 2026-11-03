"""The main window: sidebar of sections, content area, toasts and adaptive layout."""

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from daybook.const import APP_NAME
from daybook.core.errors import SchemaTooNewError
from daybook.paths import Paths
from daybook.services.context import AppContext
from daybook.services.events import Subscription, Toast
from daybook.services.safe import safe_callback
from daybook.ui.sections import Section, sections

COLLAPSE_BELOW = "max-width: 600sp"


class MainWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        application: Adw.Application,
        paths: Paths,
        context: AppContext | None,
        startup_error: BaseException | None,
    ) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.set_default_size(1100, 720)
        self.set_size_request(360, 360)
        self._paths = paths
        self._subscriptions: list[Subscription] = []

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)

        if context is None:
            self._toasts.set_child(self._build_error_page(startup_error))
            return
        self._toasts.set_child(self._build_main())
        self._subscriptions.append(context.bus.subscribe(Toast, self._on_toast))
        self.connect("destroy", self._on_destroy)

    # Layout -------------------------------------------------------------------------

    def _build_main(self) -> Gtk.Widget:
        self._sections = sections()
        self._split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)
        for section in self._sections:
            page = Adw.StatusPage(
                icon_name=section.icon,
                title=section.empty_title,
                description=section.empty_description,
            )
            self._stack.add_named(page, section.key)

        content_view = Adw.ToolbarView()
        content_view.add_top_bar(Adw.HeaderBar())
        content_view.set_content(self._stack)
        self._content_page = Adw.NavigationPage(
            title=self._sections[0].title, child=content_view, tag="content"
        )

        self._split.set_sidebar(
            Adw.NavigationPage(title=APP_NAME, child=self._build_sidebar(), tag="sidebar")
        )
        self._split.set_content(self._content_page)

        collapse = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(COLLAPSE_BELOW))
        collapse.add_setter(self._split, "collapsed", True)
        self.add_breakpoint(collapse)
        return self._split

    def _build_sidebar(self) -> Gtk.Widget:
        self._sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._sidebar.add_css_class("navigation-sidebar")
        self._sidebar.update_property([Gtk.AccessibleProperty.LABEL], [_("Sections")])
        for section in self._sections:
            self._sidebar.append(self._sidebar_row(section))
        self._sidebar.connect("row-selected", self._on_row_selected)
        self._sidebar.connect("row-activated", self._on_row_activated)
        self._sidebar.select_row(self._sidebar.get_row_at_index(0))

        menu = Gio.Menu()
        menu.append(_("_About Daybook"), "app.about")
        menu.append(_("_Quit"), "app.quit")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            primary=True,
            tooltip_text=_("Main Menu"),
        )
        header = Adw.HeaderBar()
        header.pack_end(menu_button)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(
            Gtk.ScrolledWindow(child=self._sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER)
        )
        return view

    def _sidebar_row(self, section: Section) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12)
        icon = Gtk.Image(icon_name=section.icon, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        box.append(icon)
        box.append(Gtk.Label(label=section.title, xalign=0, hexpand=True))
        row = Gtk.ListBoxRow(child=box)
        row.set_name(section.key)
        return row

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

    # Handlers -----------------------------------------------------------------------

    @safe_callback
    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        section = self._sections[row.get_index()]
        self._stack.set_visible_child_name(section.key)
        self._content_page.set_title(section.title)

    @safe_callback
    def _on_row_activated(self, _listbox: Gtk.ListBox, _row: Gtk.ListBoxRow) -> None:
        # In the collapsed (narrow) layout, choosing a section opens its page.
        self._split.set_show_content(True)

    @safe_callback
    def _on_toast(self, event: Toast) -> None:
        toast = Adw.Toast(title=event.message)
        if event.undo is not None:
            undo = event.undo
            toast.set_button_label(_("_Undo"))
            toast.set_use_markup(False)

            def on_undo(_toast: Adw.Toast) -> None:
                undo()

            toast.connect("button-clicked", safe_callback(on_undo))
        self._toasts.add_toast(toast)

    @safe_callback
    def _on_open_log_folder(self, _button: Gtk.Button) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self._paths.log_dir)))
        launcher.launch(self, None, None)

    def _on_destroy(self, _window: Gtk.Window) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
