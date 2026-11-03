"""The compact Focus window (SPEC §3, PLAN §6.3): the timed task, a large remaining-time
display with a progress ring, and Pause / Done / Next.

Wayland lets no app put itself on top; the user pins this window with GNOME's window menu
("Always on Top", D-093). It also shows live reminders as a card (D-055).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from gettext import gettext as _

import cairo
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from daybook.core.models import ProjectColor, Task
from daybook.core.timeutil import fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, ReminderShown, Subscription
from daybook.services.plan import DayPlan, PlanEntry
from daybook.services.safe import report_error, safe_callback
from daybook.ui.timer_actions import start_timer, toggle_timer
from daybook.ui.widgets.project_dot import dot, set_dot_color
from daybook.ui.widgets.reminder_card import OnAction, ReminderCard

RING_SIZE = 184
RING_WIDTH = 10
# Plans and totals are re-read this often; the clock face itself updates every second.
RELOAD_SECONDS = 30


def clock_text(seconds: int) -> str:
    """``18:04`` or ``1:02:03``; whole seconds, never negative."""
    seconds = max(0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


@dataclass(frozen=True)
class _Face:
    """What the window shows, read at `as_of`; the live part advances with the clock."""

    task: Task
    color: ProjectColor
    running: bool
    tracked_s: int  # today, at `as_of`
    as_of: datetime
    entry: PlanEntry | None
    day: DayPlan
    next_task: int | None


class FocusWindow(Adw.ApplicationWindow):
    def __init__(
        self, application: Adw.Application, context: AppContext, on_action: OnAction
    ) -> None:
        super().__init__(
            application=application,
            title=_("Focus"),
            default_width=320,
            default_height=380,
        )
        # Added, not passed as css_classes, which would drop the window's own classes.
        self.add_css_class("focus-window")
        self.set_size_request(280, 320)
        self._ctx = context
        self._on_action = on_action
        self._face: _Face | None = None
        self._planned_task: int | None = None
        self._tick_id: int | None = None
        self._reload_id: int | None = None
        self._queued = False
        self._ring_fraction: float | None = None
        self._ring_over = False
        self._subscriptions: list[Subscription] = []

        view = Adw.ToolbarView(content=self._build_body())
        view.add_top_bar(self._build_header())
        self.set_content(view)
        self._add_shortcuts()

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self._subscriptions.append(context.bus.subscribe(DayChanged, self._on_day_changed))
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.connect("close-request", self._on_close_request)
        self._reload()

    # Building ---------------------------------------------------------------------------

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar(css_classes=["flat"])
        title_box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self._dot = dot(ProjectColor.SLATE)
        self._key = Gtk.Label(css_classes=["heading"], use_markup=False)
        title_box.append(self._dot)
        title_box.append(self._key)
        header.set_title_widget(title_box)

        return header

    def _build_body(self) -> Gtk.Widget:
        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_start=18,
            margin_end=18,
            margin_bottom=18,
        )
        self._title = Gtk.Label(
            wrap=True,
            justify=Gtk.Justification.CENTER,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
            css_classes=["title-4"],
            use_markup=False,
        )
        body.append(self._title)

        self._ring = Gtk.DrawingArea(
            content_width=RING_SIZE,
            content_height=RING_SIZE,
            halign=Gtk.Align.CENTER,
            accessible_role=Gtk.AccessibleRole.PRESENTATION,
        )
        self._ring.set_draw_func(self._draw_ring)
        face = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER
        )
        self._time = Gtk.Label(css_classes=["focus-time"])
        self._caption = Gtk.Label(css_classes=["caption", "dim-label"])
        face.append(self._time)
        face.append(self._caption)
        overlay = Gtk.Overlay(child=self._ring, halign=Gtk.Align.CENTER)
        overlay.add_overlay(face)
        body.append(overlay)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._pause = Gtk.Button(css_classes=["circular", "suggested-action"])
        self._pause.connect("clicked", self._on_pause_clicked)
        self._done = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="object-select-symbolic", label=_("_Done"), use_underline=True
            ),
            tooltip_text=_("Mark the task done (Ctrl+Enter)"),
        )
        self._done.connect("clicked", self._on_done_clicked)
        self._next = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="media-skip-forward-symbolic", label=_("_Next"), use_underline=True
            ),
        )
        self._next.connect("clicked", self._on_next_clicked)
        for button in (self._pause, self._done, self._next):
            buttons.append(button)
        body.append(buttons)

        self._idle = Gtk.Label(
            wrap=True,
            justify=Gtk.Justification.CENTER,
            css_classes=["dim-label"],
            label=_("Nothing is planned now. Start a task's timer to see it here."),
            vexpand=True,
        )
        body.append(self._idle)

        # Live reminders appear here as an accent card, with the same buttons (D-055).
        self._card = ReminderCard(self._on_action)
        body.append(self._card)

        hint = Gtk.Label(
            label=_("To keep it in view, right-click the title bar → Always on Top"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            css_classes=["caption", "dim-label"],
            valign=Gtk.Align.END,
            vexpand=True,
        )
        body.append(hint)

        return body

    def _add_shortcuts(self) -> None:
        shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        for trigger, callback in (
            ("space", self._on_space),
            ("<Control>Return", self._on_ctrl_return),
            ("<Control>w", self._on_close_shortcut),
        ):
            shortcuts.add_shortcut(
                Gtk.Shortcut(
                    trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                    action=Gtk.CallbackAction.new(callback),
                )
            )
        self.add_controller(shortcuts)

    def show_reminders(self, live: Mapping[str, ReminderShown]) -> None:
        self._card.show_most_urgent(live)

    # Reading ----------------------------------------------------------------------------

    def queue_reload(self) -> None:
        if not self._queued:
            self._queued = True
            GLib.idle_add(self._reload_idle)

    @safe_callback
    def _reload_idle(self) -> bool:
        self._queued = False
        self._reload()
        return GLib.SOURCE_REMOVE

    def _reload(self) -> None:
        now = self._ctx.clock.now()
        today = self._ctx.tasks.today()
        day = self._ctx.plan.day_plan(today, now)
        timer = self._ctx.timer.state()
        task_id: int | None = None
        running = False
        if timer is not None:
            task_id, running = timer.task_id, timer.running
        else:
            segment = day.schedule.segment_at(now)
            if segment is not None:
                task_id = segment.task_id
        self._planned_task = task_id if timer is None else None
        if task_id is None:
            self._face = None
            self._show()
            return
        task = self._ctx.tasks.get(task_id)
        entry = next((e for e in day.entries if e.summary.task.id == task_id), None)
        after = day.schedule.next_task_after(now, entry.item.id if entry else None)
        self._face = _Face(
            task=task,
            color=entry.summary.project_color
            if entry
            else self._ctx.projects.get(task.project_id).color,
            running=running,
            tracked_s=self._ctx.timer.tracked_today_s(task_id),
            as_of=now,
            entry=entry,
            day=day,
            next_task=after.task_id if after else None,
        )
        self._show()

    # Showing ----------------------------------------------------------------------------

    def _show(self) -> None:
        face = self._face
        has_task = face is not None
        for widget in (self._ring.get_parent(), self._pause, self._done, self._next, self._dot):
            if widget is not None:
                widget.set_visible(has_task)
        self._idle.set_visible(not has_task)
        if face is None:
            self._key.set_text(_("Focus"))
            self._title.set_text("")
            self._arm_tick(False)
            return
        task = face.task
        set_dot_color(self._dot, face.color)
        self._key.set_text(task.ticket_key or _("Focus"))
        self._title.set_text(task.title)
        if face.running:
            icon, label = "media-playback-pause-symbolic", _("Pause (Space)")
        elif self._planned_task is not None:
            icon, label = "media-playback-start-symbolic", _("Start (Space)")
        else:
            icon, label = "media-playback-start-symbolic", _("Resume (Space)")
        self._pause.set_icon_name(icon)
        self._pause.set_tooltip_text(label)
        self._pause.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._next.set_sensitive(face.next_task is not None)
        self._next.set_tooltip_text(
            _("Start the next planned task")
            if face.next_task is not None
            else _("Nothing else is planned today")
        )
        self._arm_tick(face.running)
        self._update_clock()

    def _tracked_now(self, face: _Face, now: datetime) -> int:
        if not face.running:
            return face.tracked_s
        return face.tracked_s + max(0, int((now - face.as_of).total_seconds()))

    def _progress(self, face: _Face, now: datetime, tracked_s: int) -> float | None:
        allotted = face.entry.item.allotted_min if face.entry else None
        if face.entry is not None and allotted is not None:
            return min(1.0, tracked_s / (allotted * 60)) if allotted else 1.0
        window = face.day.window
        if face.entry is not None and window is not None:
            # A Rest-of-day item: the ring shows how far the working day has gone (D-050).
            span = (window.end - window.start).total_seconds()
            return min(1.0, max(0.0, (now - window.start).total_seconds() / span))
        return None

    def _update_clock(self) -> None:
        face = self._face
        if face is None:
            return
        now = self._ctx.clock.now()
        tracked_s = self._tracked_now(face, now)
        allotted = face.entry.item.allotted_min if face.entry else None
        if allotted is not None:
            left = allotted * 60 - tracked_s
            if left >= 0:
                self._time.set_text(clock_text(left))
                self._caption.set_text(_("left"))
            else:
                self._time.set_text("+" + clock_text(-left))
                self._caption.set_text(_("over"))
        else:
            self._time.set_text(clock_text(tracked_s))
            if face.entry is not None:
                share = face.day.schedule.rest_of_day_min.get(face.entry.item.id, 0)
                self._caption.set_text(_("today · {free} free").format(free=fmt_duration(share)))
            else:
                self._caption.set_text(_("today"))
        state = _("running") if face.running else _("paused")
        self._time.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"{self._time.get_text()} {self._caption.get_text()}, {state}"],
        )
        self._ring_fraction = self._progress(face, now, tracked_s)
        self._ring_over = allotted is not None and tracked_s > allotted * 60
        self._ring.queue_draw()

    def _draw_ring(
        self, area: Gtk.DrawingArea, cr: cairo.Context[cairo.Surface], width: int, height: int
    ) -> None:
        try:
            radius = min(width, height) / 2 - RING_WIDTH / 2 - 1
            cx, cy = width / 2, height / 2
            fg = area.get_color()
            cr.set_line_width(RING_WIDTH)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.stroke()
            fraction = self._ring_fraction
            if fraction is None or fraction <= 0:
                return
            accent: Gdk.RGBA = Adw.StyleManager.get_default().get_accent_color_rgba()
            if self._ring_over:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            else:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 1.0)
            start = -math.pi / 2
            cr.arc(cx, cy, radius, start, start + 2 * math.pi * fraction)
            cr.stroke()
        except Exception as exc:
            report_error(exc)

    # Ticking ----------------------------------------------------------------------------

    def _arm_tick(self, running: bool) -> None:
        if running and self._tick_id is None and self.get_mapped():
            second = self._ctx.clock.real_seconds_until(
                self._ctx.clock.now() + timedelta(seconds=1)
            )
            self._tick_id = GLib.timeout_add(max(100, int(second * 1000)), self._on_tick)
        elif not running and self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _on_tick(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the ticking for good.
        try:
            self._update_clock()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_CONTINUE

    def _on_reload_tick(self) -> bool:
        self.queue_reload()
        return GLib.SOURCE_CONTINUE

    # Handlers ---------------------------------------------------------------------------

    def _current(self) -> int | None:
        return self._face.task.id if self._face is not None else None

    @safe_callback
    def _on_pause_clicked(self, _button: Gtk.Button) -> None:
        task_id = self._current()
        if task_id is not None:
            toggle_timer(self, self._ctx, task_id)

    @safe_callback
    def _on_done_clicked(self, _button: Gtk.Button) -> None:
        task_id = self._current()
        if task_id is not None:
            self._ctx.tasks.complete(task_id)

    @safe_callback
    def _on_next_clicked(self, _button: Gtk.Button) -> None:
        if self._face is not None and self._face.next_task is not None:
            start_timer(self, self._ctx, self._face.next_task)

    def _on_space(self, _widget: Gtk.Widget, _args: GLib.Variant | None) -> bool:
        # Not @safe_callback: shortcut callbacks must return a bool.
        focus = self.get_focus()
        if isinstance(focus, Gtk.Button):
            return False  # Space presses the focused button
        try:
            self._on_pause_clicked(self._pause)
        except Exception as exc:
            report_error(exc)
        return True

    def _on_ctrl_return(self, _widget: Gtk.Widget, _args: GLib.Variant | None) -> bool:
        try:
            self._on_done_clicked(self._done)
        except Exception as exc:
            report_error(exc)
        return True

    def _on_close_shortcut(self, _widget: Gtk.Widget, _args: GLib.Variant | None) -> bool:
        self.close()
        return True

    def _on_changed(self, _event: Changed) -> None:
        self.queue_reload()

    def _on_day_changed(self, _event: DayChanged) -> None:
        self.queue_reload()

    @safe_callback
    def _on_map(self, _widget: Gtk.Widget) -> None:
        if self._reload_id is None:
            self._reload_id = GLib.timeout_add_seconds(RELOAD_SECONDS, self._on_reload_tick)
        self.queue_reload()

    @safe_callback
    def _on_unmap(self, _widget: Gtk.Widget) -> None:
        self._arm_tick(False)
        if self._reload_id is not None:
            GLib.source_remove(self._reload_id)
            self._reload_id = None

    @safe_callback
    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self._arm_tick(False)
        if self._reload_id is not None:
            GLib.source_remove(self._reload_id)
            self._reload_id = None
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
        return False
