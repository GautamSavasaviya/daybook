"""Timesheet (SPEC §6): one row per ticket, day or week, rounded as set in Preferences,
ready to copy or export for the PMS (D-028, D-052)."""

from datetime import date, timedelta
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk, Pango

from daybook.core import timesheet
from daybook.core.timesheet import Timesheet
from daybook.core.timeutil import fmt_date, fmt_day_month, fmt_duration, fmt_weekday
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.safe import report_error, safe_callback
from daybook.ui.export import copy_text, save_text
from daybook.ui.widgets.buttons import text_button
from daybook.ui.widgets.trim_tip import tip_if_trimmed

TICK_SECONDS = 30  # a running timer keeps adding to today's row


class TimesheetPane(Gtk.Box):
    def __init__(self, context: AppContext) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._ctx = context
        self._anchor = context.reports.today()
        self._week = False
        self._sheet: Timesheet | None = None
        self._queued = False
        self._tick_id: int | None = None
        self._subscriptions: list[Subscription] = []

        nav = Gtk.Box(spacing=6)
        self._prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Previous"))
        self._prev.update_property([Gtk.AccessibleProperty.LABEL], [_("Previous period")])
        self._prev.connect("clicked", self._on_step, -1)
        self._next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Next"))
        self._next.update_property([Gtk.AccessibleProperty.LABEL], [_("Next period")])
        self._next.connect("clicked", self._on_step, 1)
        self._title = tip_if_trimmed(
            Gtk.Label(hexpand=True, css_classes=["heading"], ellipsize=Pango.EllipsizeMode.END)
        )
        self._span = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self._span.add(Adw.Toggle(name="day", label=_("Day")))
        self._span.add(Adw.Toggle(name="week", label=_("Week")))
        self._span.set_active_name("day")
        self._span.connect("notify::active-name", self._on_span)
        today = text_button(_("Today"), self._on_today)
        for widget in (self._prev, self._next, self._title, self._span, today):
            nav.append(widget)
        self.append(nav)

        self._body = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, min_content_height=120
        )
        self._empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title=_("No Time Tracked"),
            description=_("Time you track with the timer, or add by hand, shows up here."),
            vexpand=True,
        )
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._body, "sheet")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)

        self._note = Gtk.Label(xalign=0, wrap=True, css_classes=["caption", "dim-label"])
        self.append(self._note)
        actions = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self._copy = Gtk.Button(label=_("_Copy"), use_underline=True)
        self._copy.set_tooltip_text(_("Copy as text, to paste into the timesheet"))
        self._copy.connect("clicked", self._on_copy)
        self._csv = Gtk.Button(label=_("Export _CSV…"), use_underline=True)
        self._csv.connect("clicked", self._on_csv)
        actions.append(self._copy)
        actions.append(self._csv)
        self.append(actions)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day))
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("destroy", self._on_destroy)

    # Showing ----------------------------------------------------------------------------

    def _days(self) -> tuple[date, ...]:
        return timesheet.week_days(self._anchor) if self._week else (self._anchor,)

    def queue_refresh(self) -> None:
        if self.get_mapped() and not self._queued:
            self._queued = True
            GLib.idle_add(self._refresh)

    @safe_callback
    def _refresh(self) -> bool:
        self._queued = False
        days = self._days()
        today = self._ctx.reports.today()
        self._sheet = sheet = self._ctx.reports.timesheet(days)
        self._title.set_text(
            timesheet.week_title(days)
            if self._week
            else (_("Today") if days[0] == today else fmt_date(days[0]))
        )
        self._next.set_sensitive(days[-1] < today)
        for widget in (self._copy, self._csv):
            widget.set_sensitive(not sheet.empty)
        if sheet.empty:
            self._stack.set_visible_child_name("empty")
            self._note.set_text("")
            return GLib.SOURCE_REMOVE
        self._body.set_child(self._grid(sheet))
        self._stack.set_visible_child_name("sheet")
        step = sheet.rounding_min
        rounding = _("rounded to the nearest {n} min").format(n=step) if step else _("not rounded")
        self._note.set_text(
            _("Tracked {raw}; the timesheet total is {total} ({rounding}).").format(
                raw=fmt_duration(sheet.raw_min),
                total=fmt_duration(sheet.total_min),
                rounding=rounding,
            )
        )
        return GLib.SOURCE_REMOVE

    def _grid(self, sheet: Timesheet) -> Gtk.Widget:
        grid = Gtk.Grid(
            column_spacing=18, row_spacing=10, margin_start=6, margin_end=6, margin_top=6
        )
        grid.update_property([Gtk.AccessibleProperty.LABEL], [_("Timesheet")])
        columns = len(sheet.days) + 1
        for index, day in enumerate(sheet.days):
            text = f"{fmt_weekday(day)}\n{fmt_day_month(day)}" if self._week else _("Time")
            head = Gtk.Label(
                label=text,
                xalign=1,
                css_classes=["caption-heading", "dim-label"],
                justify=Gtk.Justification.RIGHT,
            )
            grid.attach(head, index + 1, 0, 1, 1)
        total_head = Gtk.Label(label=_("Total"), xalign=1, css_classes=["caption-heading"])
        if self._week:
            grid.attach(total_head, columns, 0, 1, 1)
        for row_index, row in enumerate(sheet.rows, start=1):
            name = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            name.append(
                Gtk.Label(
                    label=row.label,
                    xalign=0,
                    wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR,
                    use_markup=False,
                    selectable=True,
                )
            )
            if row.detail:
                name.append(
                    Gtk.Label(
                        label=row.detail,
                        xalign=0,
                        wrap=True,
                        css_classes=["caption", "dim-label"],
                        use_markup=False,
                    )
                )
            grid.attach(name, 0, row_index, 1, 1)
            for index, day in enumerate(sheet.days):
                grid.attach(_cell(row.minutes.get(day, 0)), index + 1, row_index, 1, 1)
            if self._week:
                grid.attach(_cell(row.total_min, bold=True), columns, row_index, 1, 1)
        last = len(sheet.rows) + 1
        grid.attach(Gtk.Separator(), 0, last, columns + (1 if self._week else 0), 1)
        grid.attach(
            Gtk.Label(label=_("Total"), xalign=0, css_classes=["heading"]), 0, last + 1, 1, 1
        )
        for index, day in enumerate(sheet.days):
            grid.attach(_cell(sheet.day_total_min(day), bold=True), index + 1, last + 1, 1, 1)
        if self._week:
            grid.attach(_cell(sheet.total_min, bold=True), columns, last + 1, 1, 1)
        return Adw.Clamp(child=grid, maximum_size=1000, tightening_threshold=900)

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_step(self, _button: Gtk.Button, direction: int) -> None:
        self._anchor += timedelta(days=(7 if self._week else 1) * direction)
        self.queue_refresh()

    @safe_callback
    def _on_today(self, _button: Gtk.Button) -> None:
        self._anchor = self._ctx.reports.today()
        self.queue_refresh()

    @safe_callback
    def _on_span(self, group: Adw.ToggleGroup, _param: object) -> None:
        self._week = group.get_active_name() == "week"
        self.queue_refresh()

    @safe_callback
    def _on_copy(self, _button: Gtk.Button) -> None:
        if self._sheet is not None:
            copy_text(self, self._ctx, timesheet.to_text(self._sheet), _("Timesheet"))

    @safe_callback
    def _on_csv(self, _button: Gtk.Button) -> None:
        if self._sheet is None:
            return
        first, last = self._sheet.days[0], self._sheet.days[-1]
        name = f"daybook-timesheet-{first.isoformat()}" + (
            f"-{last.isoformat()}" if first != last else ""
        )
        save_text(
            self,
            self._ctx,
            timesheet.to_csv(self._sheet),
            filename=f"{name}.csv",
            title=_("Export Timesheet"),
        )

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.TIME_ENTRY, Entity.TIMER, Entity.TASK, Entity.SETTINGS):
            self.queue_refresh()

    def _on_day(self, _event: DayChanged) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add_seconds(TICK_SECONDS, self._on_tick)
        self.queue_refresh()

    def _on_tick(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the ticking for good.
        try:
            if self._ctx.timer.state() is not None:
                self.queue_refresh()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_CONTINUE

    @safe_callback
    def _on_unmap(self, _widget: Gtk.Widget) -> None:
        self._stop_tick()

    def _stop_tick(self) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        self._stop_tick()
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()


def _cell(minutes: int, *, bold: bool = False) -> Gtk.Label:
    classes = ["numeric"] + (["heading"] if bold else [])
    text = fmt_duration(minutes) if minutes else "–"
    label = Gtk.Label(label=text, xalign=1, css_classes=classes)
    if not minutes:
        label.add_css_class("dim-label")
    return label
