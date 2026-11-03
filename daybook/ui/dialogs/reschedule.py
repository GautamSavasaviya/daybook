""" "Move to…": Tomorrow or Next working day / Pick a date / Unplanned, with an optional
reason (SPEC A4, D-047, D-069)."""

from datetime import date
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core.reschedule import MoveChoice, MoveKind
from daybook.core.timeutil import fmt_date
from daybook.services.context import AppContext
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.widgets.date_button import DateButton


class MoveDialog(FormDialog):
    def __init__(self, context: AppContext, task_id: int) -> None:
        super().__init__(title=_("Move To"), primary_label=_("_Move"))
        self._ctx = context
        self._task_id = task_id
        task = context.tasks.get(task_id)
        self._quick = context.tasks.quick_move_choice()
        quick_day = context.tasks.move_target(self._quick)
        assert quick_day is not None

        options = Adw.PreferencesGroup(title=_("Move “{title}” to").format(title=short(task.title)))
        quick_title = (
            _("Tomorrow") if self._quick.kind is MoveKind.TOMORROW else _("Next working day")
        )
        self._quick_check, _row = self._option(options, quick_title, fmt_date(quick_day), None)
        self._date_check, date_row = self._option(
            options, _("Pick a date"), None, self._quick_check
        )
        self._date = DateButton(
            today=context.tasks.today,
            empty_label=_("Choose…"),
            accessible_name=_("Date to move to"),
            allow_clear=False,
            on_changed=self._on_date_picked,
        )
        date_row.add_suffix(self._date)
        self.register_field("move_date", date_row)
        self._unplanned_check, _row = self._option(
            options, _("Unplanned"), _("Take it out of the plan"), self._quick_check
        )
        self._quick_check.set_active(True)
        self.page.add(options)

        reason_group = Adw.PreferencesGroup(
            description=_("A reason is added to the task's notes with today's date.")
        )
        self._reason = Adw.EntryRow(title=_("Reason (optional)"), activates_default=True)
        reason_group.add(self._reason)
        self.register_field("move_reason", self._reason)
        self.page.add(reason_group)

    def _option(
        self,
        group: Adw.PreferencesGroup,
        title: str,
        subtitle: str | None,
        radio_group: Gtk.CheckButton | None,
    ) -> tuple[Gtk.CheckButton, Adw.ActionRow]:
        check = Gtk.CheckButton(group=radio_group, valign=Gtk.Align.CENTER)
        row = Adw.ActionRow(title=title, activatable_widget=check)
        if subtitle:
            row.set_subtitle(subtitle)
        row.add_prefix(check)
        group.add(row)
        return check, row

    @safe_callback
    def _on_date_picked(self, _value: date | None) -> None:
        self._date_check.set_active(True)

    def _choice(self) -> MoveChoice:
        if self._date_check.get_active():
            return MoveChoice(MoveKind.DATE, self._date.value)
        if self._unplanned_check.get_active():
            return MoveChoice(MoveKind.UNPLANNED)
        return self._quick

    def submit(self) -> None:
        self._ctx.tasks.move(self._task_id, self._choice(), self._reason.get_text())
