"""Shared frame for form dialogs: Cancel · title · primary button, and inline errors.

Core validation raises `ValidationError(field, message)`; a form registers which row shows
each field, so the message appears under the form and that row is marked (CLAUDE.md).
"""

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core.errors import ValidationError
from daybook.services.safe import safe_callback


class FormDialog(Adw.Dialog):
    def __init__(
        self, *, title: str, primary_label: str, width: int = 480, height: int = -1
    ) -> None:
        super().__init__(title=title, content_width=width, content_height=height)
        self._field_rows: dict[str, Gtk.Widget] = {}
        self._marked: list[Gtk.Widget] = []

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", self._on_cancel)
        header.pack_start(cancel)
        self.primary = Gtk.Button(label=primary_label, use_underline=True)
        self.primary.add_css_class("suggested-action")
        self.primary.connect("clicked", self._on_primary)
        header.pack_end(self.primary)
        self.set_default_widget(self.primary)

        self.page = Adw.PreferencesPage()
        self.error_label = Gtk.Label(
            xalign=0,
            wrap=True,
            visible=False,
            css_classes=["field-error"],
            accessible_role=Gtk.AccessibleRole.ALERT,
        )
        # Hidden until there is an error, so it leaves no gap above the form.
        self._error_group = Adw.PreferencesGroup(visible=False)
        self._error_group.add(self.error_label)
        self.page.add(self._error_group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.page)
        self.set_child(view)

    def register_field(self, field: str, row: Gtk.Widget) -> None:
        self._field_rows[field] = row

    def show_error(self, error: ValidationError) -> None:
        self.clear_error()
        self.error_label.set_text(error.message)
        self.error_label.set_visible(True)
        self._error_group.set_visible(True)
        row = self._field_rows.get(error.field)
        if row is not None:
            row.add_css_class("error")
            self._marked.append(row)
            row.grab_focus()

    def clear_error(self) -> None:
        self.error_label.set_visible(False)
        self._error_group.set_visible(False)
        for row in self._marked:
            row.remove_css_class("error")
        self._marked.clear()

    def submit(self) -> bool | None:
        """Validate and save. Raise ValidationError to show it and keep the dialog open;
        return False to keep it open without an error (e.g. while confirming)."""
        raise NotImplementedError

    def run_and_close(self, action: Callable[[], bool | None]) -> None:
        try:
            result = action()
        except ValidationError as error:
            self.show_error(error)
            return
        if result is not False:
            self.close()

    @safe_callback
    def _on_primary(self, _button: Gtk.Button) -> None:
        self.run_and_close(self.submit)

    @safe_callback
    def _on_cancel(self, _button: Gtk.Button) -> None:
        self.close()
