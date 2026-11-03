""" "Put on hold…": a reason, an optional note and an optional follow-up date (SPEC A3)."""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core import hold as hold_rules
from daybook.core.hold import HoldRequest
from daybook.core.models import HoldReason
from daybook.services.context import AppContext
from daybook.services.text import short
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.widgets.date_button import DateButton


class HoldDialog(FormDialog):
    def __init__(self, context: AppContext, task_id: int) -> None:
        super().__init__(title=_("Put on Hold"), primary_label=_("_Put on Hold"), height=620)
        self._ctx = context
        self._task_id = task_id
        task = context.tasks.get(task_id)

        reasons = Adw.PreferencesGroup(
            title=_("Why is “{title}” on hold?").format(title=short(task.title)),
            description=_("It leaves today's plan. Its timer stops."),
        )
        self._checks: dict[HoldReason, Gtk.CheckButton] = {}
        group: Gtk.CheckButton | None = None
        for reason in HoldReason:
            check = Gtk.CheckButton(group=group, valign=Gtk.Align.CENTER)
            group = group or check
            row = Adw.ActionRow(title=hold_rules.reason_label(reason), activatable_widget=check)
            row.add_prefix(check)
            reasons.add(row)
            self._checks[reason] = check
        self._checks[HoldReason.BLOCKED].set_active(True)
        self.page.add(reasons)

        details = Adw.PreferencesGroup()
        self._note = Adw.EntryRow(title=_("Note (optional)"), activates_default=True)
        details.add(self._note)
        self.register_field("hold_note", self._note)
        self._follow_up = DateButton(
            today=context.tasks.today,
            empty_label=_("None"),
            accessible_name=_("Follow up on"),
        )
        follow_row = Adw.ActionRow(
            title=_("Follow up on"),
            subtitle=_("It comes back to “Plan my day” on this date"),
        )
        follow_row.add_suffix(self._follow_up)
        details.add(follow_row)
        self.register_field("follow_up", follow_row)
        self.page.add(details)

    def _reason(self) -> HoldReason:
        for reason, check in self._checks.items():
            if check.get_active():
                return reason
        return HoldReason.OTHER

    def submit(self) -> None:
        self._ctx.tasks.put_on_hold(
            self._task_id,
            HoldRequest(self._reason(), self._note.get_text(), self._follow_up.value),
        )
