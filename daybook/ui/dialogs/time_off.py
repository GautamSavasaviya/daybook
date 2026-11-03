"""Time off (SPEC A6): add leave or a holiday, see what's coming up, remove a day."""

from datetime import date
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from daybook.core.errors import ValidationError
from daybook.core.models import TimeOffPart
from daybook.core.timeutil import fmt_date
from daybook.services.context import AppContext
from daybook.services.events import Changed, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.widgets.date_button import DateButton


def _parts() -> tuple[tuple[TimeOffPart, str], ...]:
    return (
        (TimeOffPart.FULL, _("Full day")),
        (TimeOffPart.AM, _("Morning")),
        (TimeOffPart.PM, _("Afternoon")),
    )


def part_label(part: TimeOffPart) -> str:
    return dict(_parts())[part]


class TimeOffDialog(Adw.Dialog):
    def __init__(self, context: AppContext) -> None:
        super().__init__(title=_("Time Off"), content_width=480, content_height=640)
        self._ctx = context
        self._upcoming_rows: list[Gtk.Widget] = []
        self._subscriptions: list[Subscription] = []

        page = Adw.PreferencesPage()
        add = Adw.PreferencesGroup(
            title=_("Add time off"),
            description=_(
                "Days off are not working days: nothing is planned or carried to them, and "
                "their plans move to the next working day."
            ),
        )
        today = context.tasks.today()
        self._first = DateButton(
            today=context.tasks.today,
            empty_label="",
            accessible_name=_("First day"),
            allow_clear=False,
            on_changed=self._on_dates_changed,
        )
        self._first.set_value(today)
        first_row = Adw.ActionRow(title=_("From"))
        first_row.add_suffix(self._first)
        self._last = DateButton(
            today=context.tasks.today,
            empty_label="",
            accessible_name=_("Last day"),
            allow_clear=False,
            on_changed=self._on_dates_changed,
        )
        self._last.set_value(today)
        last_row = Adw.ActionRow(title=_("To"))
        last_row.add_suffix(self._last)
        self._part = Adw.ToggleGroup(valign=Gtk.Align.CENTER, can_shrink=True)
        for value, label in _parts():
            self._part.add(Adw.Toggle(name=value.value, label=label))
        self._part.set_active_name(TimeOffPart.FULL.value)
        part_row = Adw.ActionRow(title=_("Part"), subtitle=_("Half days are single days"))
        part_row.add_suffix(self._part)
        self._note = Adw.EntryRow(title=_("Note (optional), e.g. holiday"))
        self._error = Gtk.Label(
            xalign=0,
            wrap=True,
            visible=False,
            css_classes=["field-error"],
            accessible_role=Gtk.AccessibleRole.ALERT,
        )
        self._due = Gtk.Label(
            xalign=0, wrap=True, visible=False, css_classes=["warning"], use_markup=False
        )
        for row in (first_row, last_row, part_row, self._note):
            add.add(row)
        page.add(add)
        actions = Adw.PreferencesGroup()
        button = Adw.ButtonRow(title=_("Add Time Off"), css_classes=["suggested-action"])
        button.connect("activated", self._on_add)
        actions.add(button)
        actions.add(self._error)
        actions.add(self._due)
        page.add(actions)

        self._upcoming = Adw.PreferencesGroup(title=_("Coming up"))
        self._nothing = Adw.ActionRow(title=_("No time off planned"), activatable=False)
        self._upcoming.add(self._nothing)
        page.add(self._upcoming)

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self._fill_upcoming()
        self._on_dates_changed(None)
        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self.connect("closed", self._on_closed)

    def _fill_upcoming(self) -> None:
        for row in self._upcoming_rows:
            self._upcoming.remove(row)
        self._upcoming_rows.clear()
        entries = self._ctx.time_off.upcoming()
        self._nothing.set_visible(not entries)
        for entry in entries:
            subtitle = part_label(entry.part) + (f" · {entry.note}" if entry.note else "")
            row = Adw.ActionRow(title=fmt_date(entry.day), subtitle=subtitle, use_markup=False)
            remove = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["flat"],
                tooltip_text=_("Remove"),
            )
            remove.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Remove time off on {day}").format(day=fmt_date(entry.day))],
            )
            remove.connect("clicked", self._on_remove, entry.day)
            row.add_suffix(remove)
            self._upcoming.add(row)
            self._upcoming_rows.append(row)

    @safe_callback
    def _on_dates_changed(self, _value: object) -> None:
        first, last = self._first.value, self._last.value
        if first is None or last is None or last < first:
            self._due.set_visible(False)
            return
        due = self._ctx.time_off.due_during(first, last)
        if due:
            names = ", ".join(s.task.ticket_key or s.task.title for s in due[:5])
            more = len(due) - 5
            text = _("Due while you're away: {tasks}").format(tasks=names)
            if more > 0:
                text += " " + _("and {n} more").format(n=more)
            self._due.set_text(text)
        self._due.set_visible(bool(due))

    @safe_callback
    def _on_add(self, _row: Adw.ButtonRow) -> None:
        first, last = self._first.value, self._last.value
        if first is None or last is None:
            return
        try:
            self._ctx.time_off.mark(
                first,
                last,
                TimeOffPart(self._part.get_active_name() or "full"),
                self._note.get_text(),
            )
        except ValidationError as error:
            self._error.set_text(error.message)
            self._error.set_visible(True)
            return
        self._error.set_visible(False)
        self._note.set_text("")

    @safe_callback
    def _on_remove(self, _button: Gtk.Button, day: date) -> None:
        self._ctx.time_off.remove(day)

    def _on_changed(self, event: Changed) -> None:
        if event.entity == Entity.TIME_OFF:
            GLib.idle_add(self._refill)

    @safe_callback
    def _refill(self) -> bool:
        self._fill_upcoming()
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
