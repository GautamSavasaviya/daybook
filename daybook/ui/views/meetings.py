"""Meetings: the next two weeks day by day, then every meeting series."""

from collections.abc import Callable
from datetime import date, timedelta
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from daybook.core import recurrence as recurrence_rules
from daybook.core import timeutil
from daybook.core.models import Meeting, Occurrence
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.format import clock_format
from daybook.ui.launch import open_uri
from daybook.ui.widgets.date_button import capitalised_day

DAYS_AHEAD = 14


class MeetingsView(Gtk.Box):
    def __init__(
        self,
        context: AppContext,
        *,
        on_new: Callable[[], None],
        on_meeting_now: Callable[[], None],
        on_open_occurrence: Callable[[Occurrence], None],
        on_open_series: Callable[[int], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._ctx = context
        self._on_new = on_new
        self._on_meeting_now = on_meeting_now
        self._on_open_occurrence = on_open_occurrence
        self._on_open_series = on_open_series
        self._queued = False
        self._groups: list[Adw.PreferencesGroup] = []
        self._subscriptions: list[Subscription] = []

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=12, margin_end=12)
        meeting_now = Gtk.Button(label=_("Meeting _Now"), use_underline=True)
        meeting_now.connect("clicked", self._on_meeting_now_clicked)
        new = Gtk.Button(
            label=_("_New Meeting"), use_underline=True, css_classes=["suggested-action"]
        )
        new.connect("clicked", self._on_new_clicked)
        actions.append(meeting_now)
        actions.append(new)
        self.append(actions)

        self._page = Adw.PreferencesPage(vexpand=True)
        self._empty = Adw.StatusPage(
            icon_name="camera-video-symbolic",
            title=_("No Meetings"),
            description=_("Add your standups and calls; Daybook plans your work around them."),
            vexpand=True,
        )
        empty_new = Gtk.Button(
            label=_("New Meeting"), halign=Gtk.Align.CENTER, css_classes=["pill"]
        )
        empty_new.connect("clicked", self._on_new_clicked)
        self._empty.set_child(empty_new)
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._page, "list")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("map", self._on_map)
        self.connect("destroy", self._on_destroy)

    def queue_refresh(self) -> None:
        if self.get_mapped() and not self._queued:
            self._queued = True
            GLib.idle_add(self._refresh)

    @safe_callback
    def _refresh(self) -> bool:
        self._queued = False
        for group in self._groups:
            self._page.remove(group)
        self._groups.clear()
        today = self._ctx.tasks.today()
        tz = self._ctx.clock.local_tz()
        start, _ignored = timeutil.local_day_bounds(today, tz)
        _ignored, end = timeutil.local_day_bounds(today + timedelta(days=DAYS_AHEAD - 1), tz)
        by_day: dict[date, list[Occurrence]] = {}
        for occurrence in self._ctx.meetings.occurrences(start, end):
            by_day.setdefault(occurrence.start.astimezone(tz).date(), []).append(occurrence)
        series = self._ctx.meetings.all()
        for day in sorted(by_day):
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(capitalised_day(day, today)))
            for occurrence in by_day[day]:
                group.add(self._occurrence_row(occurrence, series))
            self._add_group(group)
        repeating = [m for m in series if m.recurrence is not None]
        if repeating:
            group = Adw.PreferencesGroup(title=_("Repeating meetings"))
            for meeting in repeating:
                group.add(self._series_row(meeting))
            self._add_group(group)
        later = [m for m in series if m.recurrence is None and m.start >= end]
        if later:
            group = Adw.PreferencesGroup(title=_("Later"))
            for meeting in later:
                group.add(self._series_row(meeting))
            self._add_group(group)
        self._stack.set_visible_child_name("list" if self._groups else "empty")
        return GLib.SOURCE_REMOVE

    def _add_group(self, group: Adw.PreferencesGroup) -> None:
        self._page.add(group)
        self._groups.append(group)

    def _occurrence_row(self, occurrence: Occurrence, series: list[Meeting]) -> Gtk.Widget:
        tz = self._ctx.clock.local_tz()
        fmt = clock_format()
        meeting = next((m for m in series if m.id == occurrence.meeting_id), None)
        bits = [fmt.span(occurrence.start, occurrence.end, tz)]
        if occurrence.is_adhoc:
            bits.append(_("Meeting now"))
        elif meeting is not None and meeting.recurrence is not None:
            anchor = meeting.start.astimezone(tz).date()
            bits.append(recurrence_rules.describe(meeting.recurrence, anchor))
        if occurrence.edited:
            bits.append(_("changed for this day"))
        row = Adw.ActionRow(
            title=occurrence.title, subtitle=" · ".join(bits), use_markup=False, activatable=True
        )
        if occurrence.link:
            join = Gtk.Button(label=_("Join"), valign=Gtk.Align.CENTER)
            join.connect("clicked", self._on_join, occurrence.link)
            row.add_suffix(join)
        row.add_suffix(
            Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION)
        )
        row.connect("activated", self._on_occurrence_activated, occurrence)
        return row

    def _series_row(self, meeting: Meeting) -> Gtk.Widget:
        tz = self._ctx.clock.local_tz()
        start = meeting.start.astimezone(tz)
        when = recurrence_rules.describe(meeting.recurrence, start.date())
        if meeting.recurrence is None:
            when = timeutil.fmt_date(start.date())
        subtitle = _("{when} at {time} · {length}").format(
            when=when,
            time=clock_format().time(meeting.start, tz),
            length=timeutil.fmt_duration(meeting.duration_min),
        )
        row = Adw.ActionRow(
            title=meeting.title, subtitle=subtitle, use_markup=False, activatable=True
        )
        row.add_suffix(
            Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION)
        )
        row.connect("activated", self._on_series_activated, meeting.id)
        return row

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_occurrence_activated(self, _row: Adw.ActionRow, occurrence: Occurrence) -> None:
        self._on_open_occurrence(occurrence)

    @safe_callback
    def _on_series_activated(self, _row: Adw.ActionRow, meeting_id: int) -> None:
        self._on_open_series(meeting_id)

    @safe_callback
    def _on_join(self, _button: Gtk.Button, link: str) -> None:
        open_uri(self, link, self._ctx.bus)

    @safe_callback
    def _on_new_clicked(self, _button: Gtk.Button) -> None:
        self._on_new()

    @safe_callback
    def _on_meeting_now_clicked(self, _button: Gtk.Button) -> None:
        self._on_meeting_now()

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.MEETING, Entity.SETTINGS):
            self.queue_refresh()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
