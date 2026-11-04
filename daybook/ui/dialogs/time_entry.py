"""Add or edit a time entry by hand: a day, from and to times, and a note (SPEC §3)."""

from gettext import gettext as _

from gi.repository import Adw, GLib

from daybook.core import validation
from daybook.core.errors import OverlapError, ValidationError
from daybook.core.models import TimeEntry
from daybook.core.tracking import EntryInput
from daybook.services.context import AppContext
from daybook.services.text import short
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.format import clock_format
from daybook.ui.widgets.date_button import DateButton


class TimeEntryDialog(FormDialog):
    def __init__(self, context: AppContext, task_id: int, entry: TimeEntry | None = None) -> None:
        super().__init__(
            title=_("Edit Time") if entry else _("Add Time"),
            primary_label=_("_Save") if entry else _("_Add"),
            height=500,
        )
        self._ctx = context
        self._task_id = task_id
        self._entry = entry
        task = context.tasks.get(task_id)
        tz = context.clock.local_tz()
        fmt = clock_format()

        group = Adw.PreferencesGroup(
            # Group titles are always Pango markup: escape the task's title.
            title=GLib.markup_escape_text(task.ticket_key or short(task.title)),
            description=_("Times are on the day you pick, for example 9:00 to 10:30."),
        )
        self._day = DateButton(
            today=context.tasks.today,
            empty_label="",
            accessible_name=_("Day"),
            allow_clear=False,
        )
        self._day.set_value(entry.start.astimezone(tz).date() if entry else context.tasks.today())
        day_row = Adw.ActionRow(title=_("Day"))
        day_row.add_suffix(self._day)
        self._start = Adw.EntryRow(title=_("From"), activates_default=True)
        self._end = Adw.EntryRow(title=_("To"), activates_default=True)
        self._note = Adw.EntryRow(title=_("Note (optional)"), activates_default=True)
        if entry is not None:
            self._start.set_text(fmt.time(entry.start, tz))
            self._end.set_text(fmt.time(entry.end, tz))
            self._note.set_text(entry.note)
        for row in (day_row, self._start, self._end, self._note):
            group.add(row)
        self.register_field("day", day_row)
        self.register_field("start", self._start)
        self.register_field("start_time", self._start)
        self.register_field("end", self._end)
        self.register_field("end_time", self._end)
        self.page.add(group)
        self._start.grab_focus()

    def submit(self) -> bool | None:
        day = self._day.value
        if day is None:
            raise ValidationError("day", _("Pick a day."))
        typed = EntryInput(
            day,
            validation.parse_clock(self._start.get_text(), "start_time"),
            validation.parse_clock(self._end.get_text(), "end_time"),
            self._note.get_text(),
        )
        try:
            if self._entry is None:
                self._ctx.timer.add_entry(self._task_id, typed)
            else:
                self._ctx.timer.update_entry(self._entry.id, typed)
        except OverlapError as error:
            raise ValidationError(error.field, self._overlap_message(error)) from error
        return None

    def _overlap_message(self, error: OverlapError) -> str:
        fmt = clock_format()
        tz = self._ctx.clock.local_tz()
        span = fmt.span(error.start, error.end, tz)
        owner = self._ctx.timer.overlap_owner(error)
        if error.entry_id is None:
            return _("This overlaps the running timer on {task} ({span}).").format(
                task=owner, span=span
            )
        return _("This overlaps time logged on {task} ({span}).").format(task=owner, span=span)
