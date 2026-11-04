"""Create or edit a meeting: a new one, the whole series, or one occurrence (PLAN §5.4).

Editing a recurring meeting first asks "This occurrence" or "All occurrences"
(`ask_scope`). Changing a series' start or rule asks before discarding single-occurrence
changes that no longer match (D-013).
"""

from collections.abc import Callable
from datetime import date, datetime, time
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core import recurrence as recurrence_rules
from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.core.models import (
    Meeting,
    MeetingDraft,
    Occurrence,
    OccurrenceEdit,
    Recurrence,
    RecurrenceFreq,
)
from daybook.core.timeutil import localize
from daybook.services.context import AppContext
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.format import clock_format
from daybook.ui.widgets.buttons import Kind, button_bar, text_button
from daybook.ui.widgets.date_button import DateButton

LENGTHS = (15, 30, 45, 60)
_REPEATS: tuple[RecurrenceFreq | None, ...] = (None, *RecurrenceFreq)


def _repeat_label(freq: RecurrenceFreq | None) -> str:
    labels = {
        None: _("Doesn't repeat"),
        RecurrenceFreq.DAILY: _("Daily"),
        RecurrenceFreq.WEEKDAYS: _("Weekdays (Mon–Fri)"),
        RecurrenceFreq.WEEKLY: _("Weekly"),
        RecurrenceFreq.MONTHLY: _("Monthly"),
    }
    return labels[freq]


def ask_scope(parent: Gtk.Widget, title: str, on_choice: Callable[[bool], None]) -> None:
    """Ask whether to change one occurrence (False) or the whole series (True)."""
    alert = Adw.AlertDialog(
        heading=_("Edit Recurring Meeting"),
        body=_("Change only this occurrence of “{title}”, or every occurrence?").format(
            title=short(title)
        ),
    )
    alert.add_response("cancel", _("_Cancel"))
    alert.add_response("one", _("_This Occurrence"))
    alert.add_response("all", _("_All Occurrences"))
    alert.set_default_response("one")
    alert.set_close_response("cancel")

    @safe_callback
    def on_response(_alert: Adw.AlertDialog, response: str) -> None:
        if response in ("one", "all"):
            on_choice(response == "all")

    alert.connect("response", on_response)
    alert.present(parent)


class MeetingDialog(FormDialog):
    def __init__(
        self,
        context: AppContext,
        *,
        meeting_id: int | None = None,
        occurrence: Occurrence | None = None,
        day: date | None = None,
    ) -> None:
        self._ctx = context
        self._meeting_id = meeting_id if occurrence is None else occurrence.meeting_id
        self._occurrence = occurrence
        self._confirmed_discard = False
        if occurrence is not None:
            title = _("Edit This Occurrence")
        elif meeting_id is not None:
            title = _("Edit Meeting")
        else:
            title = _("New Meeting")
        super().__init__(title=title, primary_label=_("_Save"), height=680)
        self._tz = context.clock.local_tz()
        meeting = context.meetings.get(self._meeting_id) if self._meeting_id else None
        self._meeting = meeting

        start, values = self._initial(day)
        local_start = start.astimezone(self._tz)
        self.page.add(self._build_main(start, values))
        if occurrence is None:
            self.page.add(self._build_series(meeting, local_start.date()))
        if self._meeting_id is not None:
            self.page.add(self._build_delete())
        self.set_focus(self._title)

    def _initial(
        self, day: date | None
    ) -> tuple[datetime, tuple[str, int, str | None, tuple[int, ...]]]:
        """Start and (title, length, link, reminders) to show first."""
        if self._occurrence is not None:
            o = self._occurrence
            return o.start, (o.title, o.duration_min, o.link, o.reminder_offsets)
        if self._meeting is not None:
            m = self._meeting
            return m.start, (m.title, m.duration_min, m.link, m.reminder_offsets)
        first_day = day or self._ctx.tasks.today()
        start = localize(datetime.combine(first_day, time(10, 0)), self._tz)
        return start, ("", 30, None, self._ctx.meetings.default_offsets())

    def _build_main(
        self, start: datetime, values: tuple[str, int, str | None, tuple[int, ...]]
    ) -> Adw.PreferencesGroup:
        title_text, length, link, offsets = values
        local_start = start.astimezone(self._tz)
        context = self._ctx
        main = Adw.PreferencesGroup()
        self._title = Adw.EntryRow(title=_("Title"), text=title_text, activates_default=True)
        self._date = DateButton(
            today=context.tasks.today,
            empty_label=_("Choose…"),
            accessible_name=_("Date"),
            allow_clear=False,
        )
        self._date.set_value(local_start.date())
        # For a series this is its first day; later days follow the rule.
        series = self._occurrence is None and self._meeting is not None
        date_row = Adw.ActionRow(title=_("Starts on") if series else _("Date"))
        date_row.add_suffix(self._date)
        self._start = Adw.EntryRow(
            title=_("Start time, e.g. 10:00"),
            text=clock_format().time(start, self._tz),
        )
        self._length = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for minutes in LENGTHS:
            self._length.add(Adw.Toggle(name=str(minutes), label=f"{minutes}m"))
        length_row = Adw.ActionRow(title=_("Length"))
        length_row.add_suffix(self._length)
        self._other_length = Adw.EntryRow(title=_("Other length, e.g. 1h 30m"))
        if length in LENGTHS:
            self._length.set_active_name(str(length))
        else:
            self._length.set_active(Gtk.INVALID_LIST_POSITION)
            self._other_length.set_text(f"{length}m")
        self._link = Adw.EntryRow(title=_("Link (Meet, Teams, Zoom…)"), text=link or "")
        self._offsets = Adw.EntryRow(
            title=_("Remind me, minutes before (e.g. 10, 1)"),
            text=", ".join(str(o) for o in offsets),
        )
        for row in (
            self._title,
            date_row,
            self._start,
            length_row,
            self._other_length,
            self._link,
            self._offsets,
        ):
            main.add(row)
        for field, row in (
            ("title", self._title),
            ("start_time", self._start),
            ("duration", self._other_length),
            ("link", self._link),
            ("reminder_offsets", self._offsets),
            ("start", date_row),
        ):
            self.register_field(field, row)
        return main

    def _build_series(self, meeting: Meeting | None, anchor: date) -> Adw.PreferencesGroup:
        rule = meeting.recurrence if meeting is not None else None
        group = Adw.PreferencesGroup(title=_("Repeat"))
        self._repeat = Adw.ComboRow(
            title=_("Repeats"), model=Gtk.StringList.new([_repeat_label(f) for f in _REPEATS])
        )
        self._repeat.set_selected(_REPEATS.index(rule.freq if rule else None))
        self._repeat.connect("notify::selected", self._on_repeat_changed)
        self._interval = Adw.SpinRow.new_with_range(1, recurrence_rules.MAX_INTERVAL, 1)
        self._interval.set_title(_("Every"))
        self._interval.set_value(rule.interval if rule else 1)
        self._days_row = Adw.ActionRow(title=_("On"))
        days_box = Adw.WrapBox(child_spacing=4, line_spacing=4, valign=Gtk.Align.CENTER)
        self._day_toggles: list[Gtk.ToggleButton] = []
        chosen = rule.days if rule and rule.days else frozenset({anchor.weekday()})
        for index, name in enumerate(recurrence_rules.weekday_names()):
            toggle = Gtk.ToggleButton(label=name, active=index in chosen, css_classes=["circular"])
            self._day_toggles.append(toggle)
            days_box.append(toggle)
        self._days_row.add_suffix(days_box)
        self._until = DateButton(
            today=self._ctx.tasks.today, empty_label=_("Never"), accessible_name=_("Ends on")
        )
        self._until.set_value(rule.until if rule else None)
        until_row = Adw.ActionRow(title=_("Ends on"))
        until_row.add_suffix(self._until)
        self._until_row = until_row
        projects = self._ctx.projects.all()
        self._projects = [None, *[p.id for p in projects]]
        self._project = Adw.ComboRow(
            title=_("Project or area"),
            model=Gtk.StringList.new([_("None"), *[p.name for p in projects]]),
        )
        current = meeting.project_id if meeting is not None else None
        if current in self._projects:
            self._project.set_selected(self._projects.index(current))
        for row in (self._repeat, self._interval, self._days_row, until_row, self._project):
            group.add(row)
        self.register_field("recurrence", self._repeat)
        self._on_repeat_changed(self._repeat, None)
        return group

    def _build_delete(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        label = _("Cancel This Occurrence") if self._occurrence else _("Delete Meeting")
        group.add(button_bar(text_button(label, self._on_delete, kind=Kind.DESTRUCTIVE)))
        return group

    # Values -----------------------------------------------------------------------------

    def _start_instant(self) -> datetime:
        day = self._date.value
        if day is None:
            raise ValidationError("start", _("Pick a date."))
        clock = validation.parse_clock(self._start.get_text())
        return localize(datetime.combine(day, clock), self._tz)

    def _length_minutes(self) -> int:
        other = validation.parse_duration(self._other_length.get_text(), "duration")
        if other is not None:
            return validation.meeting_duration(other)
        name = self._length.get_active_name()
        if not name:
            raise ValidationError("duration", _("Choose a length."))
        return int(name)

    def _rule(self) -> Recurrence | None:
        freq = _REPEATS[self._repeat.get_selected()]
        if freq is None:
            return None
        days = frozenset(i for i, t in enumerate(self._day_toggles) if t.get_active())
        if freq == RecurrenceFreq.WEEKLY and not days:
            raise ValidationError("recurrence", _("Choose at least one day."))
        return Recurrence(freq, int(self._interval.get_value()), days, self._until.value)

    def _draft(self) -> MeetingDraft:
        return MeetingDraft(
            title=self._title.get_text(),
            start=self._start_instant(),
            duration_min=self._length_minutes(),
            link=self._link.get_text(),
            recurrence=self._rule(),
            reminder_offsets=validation.parse_offsets(self._offsets.get_text()),
            project_id=self._projects[self._project.get_selected()],
        )

    def submit(self) -> bool | None:
        if self._occurrence is not None:
            self._ctx.meetings.edit_occurrence(
                self._occurrence.meeting_id,
                self._occurrence.original_start,
                OccurrenceEdit(
                    title=self._title.get_text(),
                    start=self._start_instant(),
                    duration_min=self._length_minutes(),
                    link=self._link.get_text(),
                    reminder_offsets=validation.parse_offsets(self._offsets.get_text()),
                ),
            )
            return True
        draft = self._draft()
        if self._meeting_id is None:
            self._ctx.meetings.create(draft)
            return True
        discarded = self._ctx.meetings.discarded_by(self._meeting_id, draft)
        if discarded and not self._confirmed_discard:
            self._confirm_discard(discarded)
            return False
        self._ctx.meetings.update_series(self._meeting_id, draft)
        return True

    def _confirm_discard(self, count: int) -> None:
        alert = Adw.AlertDialog(
            heading=_("Discard Single-Occurrence Changes?"),
            body=_(
                "{n} change(s) to single occurrences no longer match the new times and will "
                "be discarded."
            ).format(n=count),
        )
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("save", _("_Save Anyway"))
        alert.set_response_appearance("save", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_close_response("cancel")
        alert.connect("response", self._on_discard_response)
        alert.present(self)

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_discard_response(self, _alert: Adw.AlertDialog, response: str) -> None:
        if response == "save":
            self._confirmed_discard = True
            self.run_and_close(self.submit)

    @safe_callback
    def _on_repeat_changed(self, row: Adw.ComboRow, _param: object) -> None:
        freq = _REPEATS[row.get_selected()]
        self._interval.set_visible(
            freq in (RecurrenceFreq.DAILY, RecurrenceFreq.WEEKLY, RecurrenceFreq.MONTHLY)
        )
        units = {
            RecurrenceFreq.DAILY: _("Every … days"),
            RecurrenceFreq.WEEKLY: _("Every … weeks"),
            RecurrenceFreq.MONTHLY: _("Every … months"),
        }
        self._interval.set_title(units.get(freq, _("Every")) if freq else _("Every"))
        self._days_row.set_visible(freq == RecurrenceFreq.WEEKLY)
        self._until_row.set_visible(freq is not None)

    @safe_callback
    def _on_delete(self, _button: Gtk.Button) -> None:
        self.close()
        if self._occurrence is not None:
            self._ctx.meetings.cancel_occurrence(
                self._occurrence.meeting_id, self._occurrence.original_start
            )
        elif self._meeting_id is not None:
            self._ctx.meetings.delete(self._meeting_id)


class MeetingNowDialog(FormDialog):
    """ "Meeting now" (SPEC A7): an unplanned meeting that has started or starts now."""

    STARTED = (0, 5, 10, 15, 30)

    def __init__(self, context: AppContext) -> None:
        super().__init__(title=_("Meeting Now"), primary_label=_("_Start Meeting"), height=500)
        self._ctx = context
        group = Adw.PreferencesGroup(
            description=_("The plan moves around it. End it from Today when it's over.")
        )
        self._title = Adw.EntryRow(title=_("Title (optional)"), activates_default=True)
        self._link = Adw.EntryRow(title=_("Link (optional)"))
        self._length = Adw.ToggleGroup(valign=Gtk.Align.CENTER, can_shrink=True)
        for name, label in (("15", "15m"), ("30", "30m"), ("60", "1h"), ("open", _("Unknown"))):
            self._length.add(Adw.Toggle(name=name, label=label))
        self._length.set_active_name("open")
        length_row = Adw.ActionRow(title=_("Length"))
        length_row.add_suffix(self._length)
        self._started = Adw.ToggleGroup(valign=Gtk.Align.CENTER, can_shrink=True)
        for minutes in self.STARTED:
            label = _("Now") if minutes == 0 else _("{n}m ago").format(n=minutes)
            self._started.add(Adw.Toggle(name=str(minutes), label=label))
        self._started.set_active_name("0")
        started_row = Adw.ActionRow(title=_("Started"))
        started_row.add_suffix(self._started)
        for row in (self._title, self._link, length_row, started_row):
            group.add(row)
        self.register_field("link", self._link)
        self.register_field("started", started_row)
        self.page.add(group)

        # What it does to tracked time, before it is started (D-049).
        effect = Adw.PreferencesGroup()
        self._effect = Gtk.Label(
            xalign=0,
            wrap=True,
            use_markup=False,
            visible=False,
            css_classes=["caption"],
            accessible_role=Gtk.AccessibleRole.STATUS,
        )
        effect.add(self._effect)
        self.page.add(effect)
        self._started.connect("notify::active-name", self._on_started_changed)
        self._show_effect()
        self.set_focus(self._title)

    def _show_effect(self) -> None:
        result = self._ctx.meetings.adhoc_effect(int(self._started.get_active_name() or 0))
        text = result.error or result.notice
        self._effect.set_text(text)
        self._effect.set_visible(bool(text))
        if result.error:
            self._effect.add_css_class("field-error")
        else:
            self._effect.remove_css_class("field-error")
        self.primary.set_sensitive(not result.error)

    @safe_callback
    def _on_started_changed(self, _group: Adw.ToggleGroup, _param: object) -> None:
        self._show_effect()

    def submit(self) -> bool | None:
        length = self._length.get_active_name()
        self._ctx.meetings.start_now(
            self._title.get_text(),
            self._link.get_text(),
            None if length in (None, "open") else int(length),
            int(self._started.get_active_name() or 0),
        )
        return True
