"""Today (SPEC §2, PLAN §6.3): the Now card, the capacity bar and the day's timeline.

The timeline shows meetings, lunch, time off and the planned task segments from now on,
with a "now" marker. It refreshes on every data change and about once a minute.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk, Pango

from daybook.core.calendar import Interval
from daybook.core.meetings import effective_end
from daybook.core.models import Occurrence, ProjectColor
from daybook.core.planner import Segment, SegmentKind
from daybook.core.timeutil import fmt_duration, fmt_relative
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Subscription
from daybook.services.plan import DayPlan, PlanEntry
from daybook.services.safe import safe_callback
from daybook.ui.format import clock_format
from daybook.ui.launch import open_uri
from daybook.ui.widgets.capacity_bar import CapacityBar
from daybook.ui.widgets.project_dot import dot, set_dot_color

# The view refreshes its "now" line and countdowns at least this often.
TICK_REAL_SECONDS = 30


class TodayView(Gtk.Box):
    def __init__(
        self,
        context: AppContext,
        *,
        on_open_task: Callable[[int], None],
        on_plan: Callable[[], None],
        on_meeting_now: Callable[[], None],
        on_open_meeting: Callable[[Occurrence], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._ctx = context
        self._on_open_task = on_open_task
        self._on_plan = on_plan
        self._on_meeting_now = on_meeting_now
        self._on_open_meeting = on_open_meeting
        self._tick_id: int | None = None
        self._queued = False
        self._subscriptions: list[Subscription] = []
        self._current_task: int | None = None
        self._current_meeting: Occurrence | None = None
        # What each timeline row stands for (a task id or a meeting occurrence).
        self._row_targets: dict[Gtk.ListBoxRow, int | Occurrence] = {}

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=12,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        body.append(self._build_now_card())
        self._capacity = CapacityBar()
        body.append(self._capacity)
        body.append(self._build_overflow())
        self._timeline = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list", "timeline"]
        )
        self._timeline.update_property([Gtk.AccessibleProperty.LABEL], [_("Today's timeline")])
        self._timeline.connect("row-activated", self._on_row_activated)
        body.append(self._timeline)
        clamp = Adw.Clamp(child=body, maximum_size=760)
        self.append(
            Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        )

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("destroy", self._on_destroy)

    # Building ---------------------------------------------------------------------------

    def _build_now_card(self) -> Gtk.Widget:
        card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card", "now-card"]
        )
        top = Gtk.Box(spacing=8)
        self._now_kicker = Gtk.Label(
            xalign=0, hexpand=True, css_classes=["caption-heading", "accent"]
        )
        meeting_now = Gtk.Button(label=_("Meeting _Now"), use_underline=True, css_classes=["pill"])
        meeting_now.set_tooltip_text(_("Start an unplanned meeting (Ctrl+Shift+M)"))
        meeting_now.connect("clicked", self._on_meeting_now_clicked)
        top.append(self._now_kicker)
        top.append(meeting_now)
        card.append(top)

        title_line = Gtk.Box(spacing=8)
        self._now_dot = dot(ProjectColor.SLATE)
        self._now_title = Gtk.Label(
            xalign=0, hexpand=True, wrap=True, css_classes=["title-3"], use_markup=False
        )
        title_line.append(self._now_dot)
        title_line.append(self._now_title)
        card.append(title_line)
        self._now_detail = Gtk.Label(xalign=0, wrap=True, css_classes=["numeric", "title-4"])
        card.append(self._now_detail)

        actions = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self._done_button = Gtk.Button(label=_("Mark _Done"), use_underline=True)
        self._done_button.connect("clicked", self._on_done_clicked)
        self._open_button = Gtk.Button(label=_("_Open Task"), use_underline=True)
        self._open_button.connect("clicked", self._on_open_clicked)
        self._end_button = Gtk.Button(
            label=_("_End Meeting"), use_underline=True, css_classes=["suggested-action"]
        )
        self._end_button.connect("clicked", self._on_end_clicked)
        self._join_button = Gtk.Button(label=_("_Join"), use_underline=True)
        self._join_button.connect("clicked", self._on_join_clicked)
        self._plan_button = Gtk.Button(
            label=_("_Plan Your Day"), use_underline=True, css_classes=["suggested-action"]
        )
        self._plan_button.connect("clicked", self._on_plan_clicked)
        for button in (
            self._end_button,
            self._join_button,
            self._done_button,
            self._open_button,
            self._plan_button,
        ):
            actions.append(button)
        card.append(actions)
        self._next_label = Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim-label"], use_markup=False
        )
        card.append(self._next_label)
        return card

    def _build_overflow(self) -> Gtk.Widget:
        self._overflow = Gtk.Box(spacing=12, css_classes=["card", "overflow-card"], visible=False)
        self._overflow_label = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["warning"])
        move = Gtk.Button(label=_("Move to Next Day"), valign=Gtk.Align.CENTER)
        move.connect("clicked", self._on_move_overflow)
        review = Gtk.Button(label=_("Review Plan"), valign=Gtk.Align.CENTER, css_classes=["flat"])
        review.connect("clicked", self._on_plan_clicked)
        self._overflow.append(self._overflow_label)
        self._overflow.append(move)
        self._overflow.append(review)
        return self._overflow

    # Refresh ----------------------------------------------------------------------------

    def queue_refresh(self) -> None:
        if not self.get_mapped() or self._queued:
            return
        self._queued = True
        GLib.idle_add(self._refresh)

    @safe_callback
    def _refresh(self) -> bool:
        self._queued = False
        today = self._ctx.tasks.today()
        now = self._ctx.clock.now()
        day = self._ctx.plan.day_plan(today, now)
        self._fill_now(day, now)
        self._capacity.show_schedule(day.schedule)
        self._fill_overflow(day)
        self._fill_timeline(day, now)
        return GLib.SOURCE_REMOVE

    def _fill_now(self, day: DayPlan, now: datetime) -> None:
        tz = self._ctx.clock.local_tz()
        fmt = clock_format()
        running = sorted(self._ctx.meetings.in_progress(), key=lambda o: (not o.is_adhoc, o.start))
        segment = day.schedule.segment_at(now)
        entry = day.entry(segment.plan_item_id) if segment else None
        self._current_meeting = running[0] if running else None
        self._current_task = entry.summary.task.id if entry else None
        for button in (
            self._end_button,
            self._join_button,
            self._done_button,
            self._open_button,
            self._plan_button,
        ):
            button.set_visible(False)
        self._now_dot.set_visible(False)

        if self._current_meeting is not None:
            meeting = self._current_meeting
            self._now_kicker.set_text(_("IN MEETING"))
            self._now_title.set_text(meeting.title)
            minutes = int((now - meeting.start).total_seconds() // 60)
            if meeting.open_ended:
                detail = _("{time} so far").format(time=fmt_duration(minutes))
            else:
                detail = _("{time} so far · until {end}").format(
                    time=fmt_duration(minutes), end=fmt.time(meeting.end, tz)
                )
            self._now_detail.set_text(detail)
            self._end_button.set_visible(meeting.is_adhoc)
            self._join_button.set_visible(bool(meeting.link))
        elif entry is not None and segment is not None:
            self._now_kicker.set_text(_("NOW"))
            self._show_task_title(entry)
            self._now_detail.set_text(self._task_detail(entry, day, segment))
            self._done_button.set_visible(True)
            self._open_button.set_visible(True)
        else:
            upcoming = day.schedule.next_task_after(now, None)
            self._now_kicker.set_text(_("NOW"))
            if not day.entries:
                self._now_title.set_text(_("Nothing planned yet"))
                self._now_detail.set_text(_("Pick today's tasks and give each one a time."))
                self._plan_button.set_visible(True)
            elif upcoming is not None:
                self._now_title.set_text(_("Free right now"))
                self._now_detail.set_text(
                    _("Next task at {time}").format(time=fmt.time(upcoming.start, tz))
                )
            elif not day.schedule.working:
                self._now_title.set_text(_("Not a working day"))
                self._now_detail.set_text(_("Enjoy the day off."))
            else:
                self._now_title.set_text(_("Nothing left for today"))
                self._now_detail.set_text(_("Everything planned is done or scheduled earlier."))
        self._next_label.set_text(self._next_text(day, now, segment))
        self._next_label.set_visible(bool(self._next_label.get_text()))

    def _show_task_title(self, entry: PlanEntry) -> None:
        task = entry.summary.task
        self._now_dot.set_visible(True)
        set_dot_color(self._now_dot, entry.summary.project_color)
        title = f"{task.ticket_key}  {task.title}" if task.ticket_key else task.title
        self._now_title.set_text(title)

    def _task_detail(self, entry: PlanEntry, day: DayPlan, segment: Segment) -> str:
        tz = self._ctx.clock.local_tz()
        until = _("until {time}").format(time=clock_format().time(segment.end, tz))
        if entry.item.allotted_min is None:
            share = day.schedule.rest_of_day_min.get(entry.item.id, 0)
            return _("Rest of day · {time} free today · {until}").format(
                time=fmt_duration(share), until=until
            )
        left = max(0, entry.item.allotted_min - entry.tracked_min)
        return _("{left} left today · {until}").format(left=fmt_duration(left), until=until)

    def _next_text(self, day: DayPlan, now: datetime, segment: Segment | None) -> str:
        parts: list[str] = []
        meeting = next((o for o in day.occurrences if o.start > now), None)
        if meeting is not None:
            parts.append(
                _("{title} {when}").format(
                    title=meeting.title, when=fmt_relative(meeting.start - now)
                )
            )
        after = segment.end if segment else now
        then = day.schedule.next_task_after(after, segment.plan_item_id if segment else None)
        entry = day.entry(then.plan_item_id) if then else None
        if entry is not None:
            task = entry.summary.task
            name = task.ticket_key or task.title
            amount = (
                ""
                if entry.item.allotted_min is None
                else f" ({fmt_duration(entry.item.allotted_min)})"
            )
            parts.append(_("then {task}{amount}").format(task=name, amount=amount))
        return _("Next: {items}").format(items=" · ".join(parts)) if parts else ""

    def _fill_overflow(self, day: DayPlan) -> None:
        overflow = day.schedule.overflow
        visible = bool(overflow) and day.schedule.working
        self._overflow.set_visible(visible)
        if visible:
            minutes = sum(o.minutes or 0 for o in overflow)
            self._overflow_label.set_text(
                _("{n} doesn't fit today ({time})").format(
                    n=_("1 task") if len(overflow) == 1 else _("{n} tasks").format(n=len(overflow)),
                    time=fmt_duration(minutes),
                )
            )

    def _fill_timeline(self, day: DayPlan, now: datetime) -> None:
        self._timeline.remove_all()
        self._row_targets.clear()
        rows: list[tuple[datetime, datetime, Gtk.ListBoxRow]] = []
        for segment in day.schedule.segments:
            end = segment.end
            if segment.kind == SegmentKind.MEETING:
                occurrence = next((o for o in day.occurrences if o.key == segment.key), None)
                end = effective_end(occurrence, now) if occurrence else end
            rows.append((segment.start, end, self._segment_row(segment, day, now)))
        if day.leave is not None:
            leave = self._block_row(day.leave, _("Time off"), "leave")
            rows.append((day.leave.start, day.leave.end, leave))
        rows.sort(key=lambda entry: entry[0])
        marker_placed = False
        # The "now" line goes above the first block that hasn't ended: what's on now.
        for _start, end, row in rows:
            if not marker_placed and end > now:
                self._timeline.append(self._now_row(now))
                marker_placed = True
            self._timeline.append(row)
        if not marker_placed and rows:
            self._timeline.append(self._now_row(now))
        if not rows:
            empty = Adw.ActionRow(
                title=_("Nothing on the timeline yet"),
                subtitle=_("Meetings and planned tasks appear here."),
                activatable=False,
            )
            self._timeline.append(empty)

    def _row(
        self, time_text: str, kind: str, title: str, subtitle: str, suffix: Gtk.Widget | None
    ) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        time_label = Gtk.Label(
            label=time_text, xalign=0, width_chars=6, css_classes=["numeric", "dim-label"]
        )
        bar = Gtk.Box(
            css_classes=["timeline-bar", kind], accessible_role=Gtk.AccessibleRole.PRESENTATION
        )
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.append(
            Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=False)
        )
        if subtitle:
            text.append(
                Gtk.Label(
                    label=subtitle,
                    xalign=0,
                    wrap=True,
                    css_classes=["caption", "dim-label"],
                    use_markup=False,
                )
            )
        for widget in (time_label, bar, text):
            box.append(widget)
        if suffix is not None:
            box.append(suffix)
        row = Gtk.ListBoxRow(child=box)
        row.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [", ".join(p for p in (time_text, title, subtitle) if p)],
        )
        return row

    def _segment_row(self, segment: Segment, day: DayPlan, now: datetime) -> Gtk.ListBoxRow:
        tz = self._ctx.clock.local_tz()
        fmt = clock_format()
        start_text = fmt.time(segment.start, tz)
        if segment.kind == SegmentKind.LUNCH:
            row = self._row(start_text, "lunch", _("Lunch"), fmt_duration(segment.minutes), None)
            row.set_activatable(False)
            return row
        if segment.kind == SegmentKind.MEETING:
            occurrence = next((o for o in day.occurrences if o.key == segment.key), None)
            end = effective_end(occurrence, now) if occurrence else segment.end
            subtitle = fmt.span(segment.start, end, tz)
            if occurrence is not None and occurrence.open_ended:
                subtitle = _("started {time} · still going").format(time=start_text)
            join = None
            if occurrence is not None and occurrence.link:
                join = Gtk.Button(label=_("Join"), valign=Gtk.Align.CENTER)
                join.connect("clicked", self._on_join_link, occurrence.link)
            row = self._row(start_text, "meeting", segment.title, subtitle, join)
            if end <= now:
                row.add_css_class("past")
            if occurrence is not None:
                self._row_targets[row] = occurrence
            return row
        entry = day.entry(segment.plan_item_id)
        assert entry is not None
        task = entry.summary.task
        bits = [fmt.span(segment.start, segment.end, tz), fmt_duration(segment.minutes)]
        if segment.part > 1:
            bits.append(_("part {n}").format(n=segment.part))
        if entry.item.carry_count:
            bits.append(_("carried ×{n}").format(n=entry.item.carry_count))
        if task.requested_by:
            bits.append(_("from {name}").format(name=task.requested_by))
        title = f"{task.ticket_key}  {task.title}" if task.ticket_key else task.title
        row = self._row(start_text, "task", title, " · ".join(bits), None)
        self._row_targets[row] = task.id
        return row

    def _block_row(self, span: Interval, title: str, kind: str) -> Gtk.ListBoxRow:
        tz = self._ctx.clock.local_tz()
        fmt = clock_format()
        row = self._row(
            fmt.time(span.start, tz), kind, title, fmt.span(span.start, span.end, tz), None
        )
        row.set_activatable(False)
        return row

    def _now_row(self, now: datetime) -> Gtk.ListBoxRow:
        tz = self._ctx.clock.local_tz()
        box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=2, margin_bottom=2)
        label = Gtk.Label(
            label=clock_format().time(now, tz),
            width_chars=6,
            xalign=0,
            css_classes=["numeric", "accent", "caption-heading"],
        )
        line = Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER, css_classes=["now-line"])
        box.append(label)
        box.append(line)
        row = Gtk.ListBoxRow(
            child=box, activatable=False, selectable=False, css_classes=["now-row"]
        )
        row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Now, {time}").format(time=label.get_text())]
        )
        return row

    # Ticking ----------------------------------------------------------------------------

    def _arm_tick(self) -> None:
        if self._tick_id is not None:
            return
        # With fast dev time (DAYBOOK_TIME_SCALE) a fake minute is less than 30 real seconds.
        one_minute = self._ctx.clock.real_seconds_until(
            self._ctx.clock.now() + timedelta(minutes=1)
        )
        interval = max(1, min(TICK_REAL_SECONDS, int(one_minute)))
        self._tick_id = GLib.timeout_add_seconds(interval, self._on_tick)

    def _on_tick(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the ticking for good.
        self.queue_refresh()
        return GLib.SOURCE_CONTINUE

    # Handlers ---------------------------------------------------------------------------

    def _on_changed(self, _event: Changed) -> None:
        self.queue_refresh()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.queue_refresh()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        self._arm_tick()
        self.queue_refresh()

    @safe_callback
    def _on_unmap(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    @safe_callback
    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        target = self._row_targets.get(row)
        if isinstance(target, int):
            self._on_open_task(target)
        elif isinstance(target, Occurrence):
            self._on_open_meeting(target)

    @safe_callback
    def _on_meeting_now_clicked(self, _button: Gtk.Button) -> None:
        self._on_meeting_now()

    @safe_callback
    def _on_plan_clicked(self, _button: Gtk.Button) -> None:
        self._on_plan()

    @safe_callback
    def _on_done_clicked(self, _button: Gtk.Button) -> None:
        if self._current_task is not None:
            self._ctx.tasks.complete(self._current_task)

    @safe_callback
    def _on_open_clicked(self, _button: Gtk.Button) -> None:
        if self._current_task is not None:
            self._on_open_task(self._current_task)

    @safe_callback
    def _on_end_clicked(self, _button: Gtk.Button) -> None:
        if self._current_meeting is not None:
            self._ctx.meetings.end_now(self._current_meeting.meeting_id)

    @safe_callback
    def _on_join_clicked(self, _button: Gtk.Button) -> None:
        if self._current_meeting is not None and self._current_meeting.link:
            self._launch(self._current_meeting.link)

    @safe_callback
    def _on_join_link(self, _button: Gtk.Button, link: str) -> None:
        self._launch(link)

    def _launch(self, link: str) -> None:
        open_uri(self, link, self._ctx.bus)

    @safe_callback
    def _on_move_overflow(self, _button: Gtk.Button) -> None:
        self._ctx.plan.move_overflow(self._ctx.tasks.today())

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
