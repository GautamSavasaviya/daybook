"""Weekly report (SPEC §6): hours per project and day, planned against actual per task,
meeting against focus time, and what was carried over or moved. Computed on the worker."""

from datetime import timedelta
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, GLib, Gtk, Pango

from daybook.core import timesheet
from daybook.core.reports import TaskAccuracy, WeeklyReport
from daybook.core.timeutil import fmt_duration, fmt_weekday
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.widgets.bar_chart import Bar, BarChart
from daybook.ui.widgets.buttons import text_button
from daybook.ui.widgets.trim_tip import tip_if_trimmed


def _hours(seconds: int) -> str:
    return fmt_duration(seconds // 60)


def _minutes(minutes: int) -> str:
    return fmt_duration(minutes)


_WATCHED = (Entity.TIME_ENTRY, Entity.TIMER, Entity.TASK, Entity.PLAN, Entity.MEETING)


class WeeklyPane(Gtk.Box):
    def __init__(self, context: AppContext) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._ctx = context
        self._anchor = context.reports.today()
        self._queued = False
        self._generation = 0
        self._subscriptions: list[Subscription] = []

        nav = Gtk.Box(spacing=6)
        self._prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Previous week"))
        self._prev.update_property([Gtk.AccessibleProperty.LABEL], [_("Previous week")])
        self._prev.connect("clicked", self._on_step, -1)
        self._next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Next week"))
        self._next.update_property([Gtk.AccessibleProperty.LABEL], [_("Next week")])
        self._next.connect("clicked", self._on_step, 1)
        self._title = tip_if_trimmed(
            Gtk.Label(hexpand=True, css_classes=["heading"], ellipsize=Pango.EllipsizeMode.END)
        )
        this_week = text_button(_("This Week"), self._on_this_week)
        for widget in (self._prev, self._next, self._title, this_week):
            nav.append(widget)
        self.append(nav)

        self._page = Adw.PreferencesPage(vexpand=True)
        self._groups: list[Adw.PreferencesGroup] = []
        self._empty = Adw.StatusPage(
            icon_name="daybook-reports-symbolic",
            title=_("Nothing This Week"),
            description=_("Track some time or finish a task, and the week adds up here."),
            vexpand=True,
        )
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._page, "report")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day))
        self.connect("map", self._on_map)
        self.connect("destroy", self._on_destroy)

    def queue_refresh(self) -> None:
        if self.get_mapped() and not self._queued:
            self._queued = True
            GLib.idle_add(self._refresh)

    @safe_callback
    def _refresh(self) -> bool:
        self._queued = False
        days = self._ctx.reports.week_of(self._anchor)
        today = self._ctx.reports.today()
        self._title.set_text(timesheet.week_title(days))
        self._next.set_sensitive(days[-1] < today)
        self._generation += 1
        generation = self._generation
        self._ctx.reports.weekly(days, lambda report: self._show(generation, report))
        return GLib.SOURCE_REMOVE

    def _show(self, generation: int, report: WeeklyReport) -> None:
        if generation != self._generation:
            return  # a newer week is on its way
        for group in self._groups:
            self._page.remove(group)
        self._groups.clear()
        if report.empty:
            self._stack.set_visible_child_name("empty")
            return
        for built in (
            self._summary(report),
            self._projects(report),
            self._days(report),
            self._focus(report),
            self._accuracy(report),
        ):
            if built is not None:
                self._page.add(built)
                self._groups.append(built)
        self._stack.set_visible_child_name("report")

    # Groups -----------------------------------------------------------------------------

    def _summary(self, report: WeeklyReport) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Summary"))
        rows = (
            (_("Tracked"), _hours(report.tracked_s)),
            (
                _("Tasks completed"),
                str(report.completed),
            ),
            (
                _("Carried over"),
                ngettext("{n} time", "{n} times", report.carried).format(n=report.carried),
            ),
            (_("Tasks planned"), str(report.planned_tasks)),
        )
        for title, value in rows:
            row = Adw.ActionRow(title=title, activatable=False)
            row.add_suffix(Gtk.Label(label=value, css_classes=["numeric"], use_markup=False))
            group.add(row)
        return group

    def _projects(self, report: WeeklyReport) -> Adw.PreferencesGroup | None:
        if not report.per_project:
            return None
        group = Adw.PreferencesGroup(title=_("Hours per Project or Area"))
        chart = BarChart(_("Hours per project or area"))
        chart.set_bars([Bar(p.name, p.seconds, _hours(p.seconds)) for p in report.per_project])
        group.add(chart)
        return group

    def _days(self, report: WeeklyReport) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Hours per Day"))
        chart = BarChart(_("Hours per day"))
        chart.set_bars(
            [
                Bar(fmt_weekday(d), s, _hours(s))
                for d, s in zip(report.days, report.per_day_s, strict=True)
            ]
        )
        group.add(chart)
        return group

    def _focus(self, report: WeeklyReport) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Focus and Meetings"),
            description=_("Unplanned “Meeting now” time is shown apart from scheduled meetings."),
        )
        chart = BarChart(_("Focus and meeting hours"))
        chart.set_bars(
            [
                Bar(_("Focus"), report.focus_min, _minutes(report.focus_min)),
                Bar(
                    _("Meetings"), report.meeting_min, fmt_duration(report.meeting_min), muted=True
                ),
                Bar(
                    _("Unplanned meetings"),
                    report.adhoc_min,
                    fmt_duration(report.adhoc_min),
                    muted=True,
                ),
            ]
        )
        group.add(chart)
        return group

    def _accuracy(self, report: WeeklyReport) -> Adw.PreferencesGroup | None:
        if not report.tasks:
            return None
        group = Adw.PreferencesGroup(
            title=_("Planned and Actual"),
            description=_("Time planned for each task this week, against time tracked."),
        )
        for task in report.tasks:
            group.add(_accuracy_row(task))
        return group

    # Handlers ---------------------------------------------------------------------------

    @safe_callback
    def _on_step(self, _button: Gtk.Button, direction: int) -> None:
        self._anchor += timedelta(days=7 * direction)
        self.queue_refresh()

    @safe_callback
    def _on_this_week(self, _button: Gtk.Button) -> None:
        self._anchor = self._ctx.reports.today()
        self.queue_refresh()

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (
            Entity.TIME_ENTRY,
            Entity.TIMER,
            Entity.TASK,
            Entity.PLAN,
            Entity.MEETING,
        ):
            self.queue_refresh()

    def _on_day(self, _event: DayChanged) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()


def _accuracy_row(task: TaskAccuracy) -> Adw.ActionRow:
    actual = fmt_duration(task.actual_min)
    if task.rest_of_day:
        detail = _("rest of day · {actual} tracked").format(actual=actual)
    elif task.planned_min:
        detail = _("{planned} planned · {actual} tracked").format(
            planned=fmt_duration(task.planned_min), actual=actual
        )
    else:
        detail = _("not planned · {actual} tracked").format(actual=actual)
    row = Adw.ActionRow(title=task.label, subtitle=detail, use_markup=False, title_lines=0)
    if task.planned_min and not task.rest_of_day:
        over = task.over_min
        text = (
            _("{time} over").format(time=fmt_duration(over))
            if over > 0
            else _("{time} under").format(time=fmt_duration(-over))
        )
        chip = Gtk.Label(
            label=text, css_classes=["chip", "caption", "warning" if over > 0 else "success"]
        )
        chip.set_valign(Gtk.Align.CENTER)
        row.add_suffix(chip)
    return row
