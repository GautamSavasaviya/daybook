"""Preferences pages that edit settings: General (hours, planning, background) and
Notifications. Every change is validated by `core/settings.py` and saved at once."""

from collections.abc import Callable
from dataclasses import replace
from datetime import time
from gettext import gettext as _
from typing import Any, cast

from gi.repository import Adw, GLib, Gtk

from daybook.const import APP_ID
from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.services.context import AppContext
from daybook.services.notifier import diagnose
from daybook.services.safe import safe_callback
from daybook.ui.widgets.buttons import button_bar, text_button

ROUNDING = (0, 5, 6, 10, 15, 30, 60)
DAYS = (_("Mon"), _("Tue"), _("Wed"), _("Thu"), _("Fri"), _("Sat"), _("Sun"))
ShowToast = Callable[[str], None]


def workdays_row(
    selected: frozenset[int], on_toggled: Callable[[Gtk.ToggleButton], None] | None = None
) -> tuple[Adw.PreferencesRow, list[Gtk.ToggleButton]]:
    """ "Workdays" with a button per weekday. The buttons sit under the label, so the label
    is never squeezed by them on a narrow window."""
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
        margin_top=10,
        margin_bottom=10,
        margin_start=12,
        margin_end=12,
    )
    box.append(Gtk.Label(label=_("Workdays"), xalign=0))
    wrap = Adw.WrapBox(child_spacing=4, line_spacing=4)
    buttons: list[Gtk.ToggleButton] = []
    for index, name in enumerate(DAYS):
        button = Gtk.ToggleButton(label=name, active=index in selected)
        if on_toggled is not None:
            button.connect("toggled", on_toggled)
        buttons.append(button)
        wrap.append(button)
    box.append(wrap)
    return Adw.PreferencesRow(child=box, activatable=False), buttons


def _clock(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


class _SettingsPage(Adw.PreferencesPage):
    def __init__(self, context: AppContext, toast: ShowToast, title: str, icon: str) -> None:
        super().__init__(title=title, icon_name=icon)
        self._ctx = context
        self._toast = toast

    def _save(self, field: str, value: object) -> bool:
        changes = cast("dict[str, Any]", {field: value})
        try:
            self._ctx.settings.save(replace(self._ctx.settings.current, **changes))
        except ValidationError as error:
            self._toast(error.message)
            return False
        return True

    def _switch(self, group: Adw.PreferencesGroup, field: str, title: str, subtitle: str) -> None:
        row = Adw.SwitchRow(
            title=title, subtitle=subtitle, active=getattr(self._ctx.settings.current, field)
        )
        row.connect("notify::active", self._on_switch, field)
        group.add(row)

    def _spin(
        self,
        group: Adw.PreferencesGroup,
        field: str,
        title: str,
        subtitle: str,
        limits: tuple[int, int],
    ) -> None:
        spin = Adw.SpinRow.new_with_range(limits[0], limits[1], 1)
        spin.set_title(title)
        spin.set_subtitle(subtitle)
        spin.set_value(getattr(self._ctx.settings.current, field))
        spin.connect("notify::value", self._on_spin, field)
        group.add(spin)

    def _clock_row(self, group: Adw.PreferencesGroup, field: str, title: str) -> None:
        row = Adw.EntryRow(title=title, show_apply_button=True)
        row.set_text(_clock(getattr(self._ctx.settings.current, field)))
        row.connect("apply", self._on_clock, field)
        group.add(row)

    @safe_callback
    def _on_switch(self, row: Adw.SwitchRow, _param: object, field: str) -> None:
        self._save(field, row.get_active())

    @safe_callback
    def _on_spin(self, row: Adw.SpinRow, _param: object, field: str) -> None:
        self._save(field, int(row.get_value()))

    @safe_callback
    def _on_clock(self, row: Adw.EntryRow, field: str) -> None:
        try:
            value = validation.parse_clock(row.get_text(), field)
        except ValidationError as error:
            row.add_css_class("error")
            self._toast(error.message)
            return
        row.remove_css_class("error")
        if self._save(field, value):
            row.set_text(_clock(value))


class GeneralPage(_SettingsPage):
    def __init__(self, context: AppContext, toast: ShowToast, app_id: str) -> None:
        super().__init__(context, toast, _("General"), "preferences-system-symbolic")
        self._app_id = app_id
        self.add(self._hours())
        self.add(self._planning())
        self.add(self._background())

    def _hours(self) -> Adw.PreferencesGroup:
        current = self._ctx.settings.current
        group = Adw.PreferencesGroup(
            title=_("Working Hours"),
            description=_("Daybook plans inside these hours, around lunch and meetings."),
        )
        self._clock_row(group, "work_start", _("Start of work (e.g. 9:00)"))
        self._clock_row(group, "work_end", _("End of work (e.g. 18:00)"))
        days, self._day_buttons = workdays_row(current.workdays, self._on_day)
        group.add(days)
        self._clock_row(group, "lunch_start", _("Lunch starts (e.g. 13:00)"))
        self._spin(
            group, "lunch_minutes", _("Lunch Length"), _("Minutes; 0 for no lunch"), (0, 240)
        )
        return group

    def _planning(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Planning and Time"))
        self._switch(
            group,
            "auto_carry_over",
            _("Carry Unfinished Work Over"),
            _("Add it to the next working day's plan automatically"),
        )
        self._spin(
            group,
            "adhoc_default_min",
            _("Length of an Unknown Meeting"),
            _("Minutes the plan makes room for, until you end it"),
            (5, 480),
        )
        current = self._ctx.settings.current.timesheet_rounding_min
        labels = [_("Off") if m == 0 else _("{n} min").format(n=m) for m in ROUNDING]
        rounding = Adw.ComboRow(
            title=_("Timesheet Rounding"),
            subtitle=_("Each day's time per ticket rounds to the nearest step"),
            model=Gtk.StringList.new(labels),
            selected=ROUNDING.index(current) if current in ROUNDING else 0,
        )
        rounding.connect("notify::selected", self._on_rounding)
        group.add(rounding)
        return group

    def _background(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Background"),
            description=_(
                "Reminders need Daybook to be running. Closing the window can leave it "
                "running in the background; Quit stops it."
            ),
        )
        self._switch(
            group,
            "run_in_background",
            _("Keep Running When the Window Closes"),
            _("Reminders and the tray icon keep working"),
        )
        self._switch(
            group,
            "start_at_login",
            _("Start at Login"),
            _("Starts Daybook without a window when you log in"),
        )
        self._spin(
            group,
            "idle_threshold_min",
            _("Away After"),
            _("Minutes without input while a timer runs; Daybook asks about the time"),
            (1, 240),
        )
        idle = self._ctx.idle
        working = idle.monitor_available
        status = Adw.ActionRow(
            title=_("Idle detection works") if working else _("Idle detection isn't available"),
            subtitle=_(
                "Daybook asks about time you were away, and notices a locked screen."
                if working
                else _("GNOME's idle monitor isn't reachable. A locked screen is still noticed.")
            ),
            subtitle_lines=0,
        )
        icon = "object-select-symbolic" if working else "dialog-warning-symbolic"
        status.add_prefix(
            Gtk.Image(
                icon_name=icon,
                css_classes=["success" if working else "warning"],
                accessible_role=Gtk.AccessibleRole.PRESENTATION,
            )
        )
        group.add(status)
        return group

    @safe_callback
    def _on_day(self, _button: Gtk.ToggleButton) -> None:
        chosen = frozenset(i for i, b in enumerate(self._day_buttons) if b.get_active())
        if self._save("workdays", chosen):
            return
        # Refused (no day left): put the buttons back as they were.
        current = self._ctx.settings.current.workdays
        for index, button in enumerate(self._day_buttons):
            button.handler_block_by_func(self._on_day)
            button.set_active(index in current)
            button.handler_unblock_by_func(self._on_day)

    @safe_callback
    def _on_rounding(self, row: Adw.ComboRow, _param: object) -> None:
        self._save("timesheet_rounding_min", ROUNDING[row.get_selected()])


class NotificationsPage(_SettingsPage):
    def __init__(self, context: AppContext, toast: ShowToast, app_id: str) -> None:
        super().__init__(
            context, toast, _("Notifications"), "preferences-system-notifications-symbolic"
        )
        self._app_id = app_id
        self.add(self._delivery())
        self.add(self._reminders())

    def _delivery(self) -> Adw.PreferencesGroup:
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
        group.add(button_bar(text_button(_("Send Test Reminder"), self._on_test)))
        return group

    def _reminders(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Reminders"))
        self._switch(
            group,
            "sound_enabled",
            _("Sound"),
            _("Not while Do Not Disturb hides banners, except for urgent reminders"),
        )
        self._switch(
            group,
            "auto_pause_meetings",
            _("Pause the Timer in Meetings"),
            _("When a meeting starts; Daybook asks to resume when it ends"),
        )
        self._switch(
            group,
            "meeting_reminders_on_leave",
            _("Meeting Reminders on Days Off"),
            _("Remind about meetings on leave and holidays too"),
        )
        self._spin(
            group,
            "allotment_warning_min",
            _("Warn Before Time Is Up"),
            _("Minutes left on a planned task"),
            (1, 120),
        )
        self._spin(
            group,
            "drift_after_min",
            _("Nudge When Idle"),
            _("Minutes without a timer during planned work"),
            (1, 120),
        )
        self._offsets = Adw.EntryRow(
            title=_("Reminders for new meetings, minutes before (e.g. 10, 1)"),
            show_apply_button=True,
        )
        current = self._ctx.settings.current
        self._offsets.set_text(", ".join(str(m) for m in current.meeting_offsets))
        self._offsets.connect("apply", self._on_offsets)
        group.add(self._offsets)
        return group

    @safe_callback
    def _on_offsets(self, row: Adw.EntryRow) -> None:
        try:
            offsets = validation.reminder_offsets(validation.parse_offsets(row.get_text()))
        except ValidationError as error:
            row.add_css_class("error")
            self._toast(error.message)
            return
        row.remove_css_class("error")
        if self._save("meeting_offsets", offsets):
            row.set_text(", ".join(str(m) for m in offsets))

    @safe_callback
    def _on_test(self, _button: Gtk.Button) -> None:
        self._ctx.reminders.send_test()
        self._toast(_("Test reminder sent"))
