"""Preferences (Ctrl+,). Phase 4 ships the Notifications page: whether notifications can
work here, a test reminder, and the reminder settings. Phase 7 adds the other pages."""

from dataclasses import replace
from gettext import gettext as _
from typing import Any, cast

from gi.repository import Adw, GLib, Gtk

from daybook.const import APP_ID
from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.services.context import AppContext
from daybook.services.notifier import diagnose
from daybook.services.safe import safe_callback


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, context: AppContext, app_id: str) -> None:
        super().__init__(title=_("Preferences"))
        self._ctx = context
        self._app_id = app_id
        page = Adw.PreferencesPage(
            title=_("Notifications"), icon_name="preferences-system-notifications-symbolic"
        )
        page.add(self._build_delivery())
        page.add(self._build_reminders())
        self.add(page)

    def _build_delivery(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Delivery"),
            description=_(
                "Meeting reminders and “time's up” stay on screen until you act. Others "
                "hide after a few seconds and wait in the notification list."
            ),
        )
        problems = diagnose(self._app_id, devel=self._app_id != APP_ID)
        status = Adw.ActionRow(
            title=_("Notifications can't work") if problems else _("Notifications work"),
            subtitle=GLib.markup_escape_text(
                " ".join(problems) or _("Daybook is installed and GNOME accepts its reminders.")
            ),
            subtitle_lines=0,
        )
        icon = "dialog-warning-symbolic" if problems else "object-select-symbolic"
        status.add_prefix(
            Gtk.Image(
                icon_name=icon,
                css_classes=["warning" if problems else "success"],
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        group.add(status)
        test = Adw.ButtonRow(title=_("Send Test Reminder"))
        test.connect("activated", self._on_test)
        group.add(test)
        return group

    def _build_reminders(self) -> Adw.PreferencesGroup:
        current = self._ctx.settings.current
        group = Adw.PreferencesGroup(title=_("Reminders"))
        for field, title, subtitle in (
            (
                "sound_enabled",
                _("Sound"),
                _("Not while Do Not Disturb hides banners, except for urgent reminders"),
            ),
            (
                "auto_pause_meetings",
                _("Pause the Timer in Meetings"),
                _("When a meeting starts; Daybook asks to resume when it ends"),
            ),
            (
                "meeting_reminders_on_leave",
                _("Meeting Reminders on Days Off"),
                _("Remind about meetings on leave and holidays too"),
            ),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=getattr(current, field))
            row.connect("notify::active", self._on_switch, field)
            group.add(row)
        for field, title, subtitle in (
            (
                "allotment_warning_min",
                _("Warn Before Time Is Up"),
                _("Minutes left on a planned task"),
            ),
            (
                "drift_after_min",
                _("Nudge When Idle"),
                _("Minutes without a timer during planned work"),
            ),
        ):
            spin = Adw.SpinRow.new_with_range(1, 120, 1)
            spin.set_title(title)
            spin.set_subtitle(subtitle)
            spin.set_value(getattr(current, field))
            spin.connect("notify::value", self._on_spin, field)
            group.add(spin)
        self._offsets = Adw.EntryRow(
            title=_("Reminders for new meetings, minutes before (e.g. 10, 1)"),
            show_apply_button=True,
        )
        self._offsets.set_text(", ".join(str(m) for m in current.meeting_offsets))
        self._offsets.connect("apply", self._on_offsets)
        group.add(self._offsets)
        return group

    def _save(self, field: str, value: object) -> bool:
        changes = cast("dict[str, Any]", {field: value})
        try:
            self._ctx.settings.save(replace(self._ctx.settings.current, **changes))
        except ValidationError as error:
            self.add_toast(Adw.Toast(title=error.message, use_markup=False))
            return False
        return True

    @safe_callback
    def _on_switch(self, row: Adw.SwitchRow, _param: object, field: str) -> None:
        self._save(field, row.get_active())

    @safe_callback
    def _on_spin(self, row: Adw.SpinRow, _param: object, field: str) -> None:
        self._save(field, int(row.get_value()))

    @safe_callback
    def _on_offsets(self, row: Adw.EntryRow) -> None:
        try:
            offsets = validation.reminder_offsets(validation.parse_offsets(row.get_text()))
        except ValidationError as error:
            row.add_css_class("error")
            self.add_toast(Adw.Toast(title=error.message, use_markup=False))
            return
        row.remove_css_class("error")
        if self._save("meeting_offsets", offsets):
            row.set_text(", ".join(str(m) for m in offsets))

    @safe_callback
    def _on_test(self, _row: Adw.ButtonRow) -> None:
        self._ctx.reminders.send_test()
        self.add_toast(Adw.Toast(title=_("Test reminder sent")))
