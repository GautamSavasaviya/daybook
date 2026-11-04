"""The one task row used by every task list (CLAUDE.md, PLAN §6.3).

Layout: done check · project dot · [ticket key] title, then a line with the project name
and the chips. Rows are recycled by the ListView, so `bind()` resets everything.
"""

from collections.abc import Callable
from datetime import date, tzinfo
from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango

from daybook.core.chips import task_chips
from daybook.core.models import ProjectColor, TaskStatus, TaskSummary
from daybook.services.safe import safe_callback
from daybook.ui.widgets.chips import chip_widget
from daybook.ui.widgets.project_dot import dot, set_dot_color
from daybook.ui.widgets.trim_tip import tip_if_trimmed


class TaskRow(Gtk.Box):
    def __init__(self, on_done_toggled: Callable[[int, bool], None]) -> None:
        super().__init__(spacing=12, css_classes=["task-row"])
        self._on_done_toggled = on_done_toggled
        self._task_id: int | None = None
        self._binding = False

        self._check = Gtk.CheckButton(valign=Gtk.Align.CENTER, css_classes=["selection-mode"])
        self._check.connect("toggled", self._on_toggled)
        self.append(self._check)

        self._dot = dot(ProjectColor.SLATE)
        self.append(self._dot)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title_line = Gtk.Box(spacing=8)
        self._ticket = Gtk.Label(css_classes=["ticket", "dim-label", "caption-heading"])
        self._title = tip_if_trimmed(
            Gtk.Label(
                xalign=0,
                hexpand=True,
                ellipsize=Pango.EllipsizeMode.END,
                css_classes=["task-title"],
                use_markup=False,
            )
        )
        title_line.append(self._ticket)
        title_line.append(self._title)
        text.append(title_line)

        self._meta = Adw.WrapBox(child_spacing=6, line_spacing=4)
        text.append(self._meta)
        self.append(text)

    @property
    def task_id(self) -> int | None:
        return self._task_id

    def bind(self, summary: TaskSummary, today: date, tz: tzinfo, *, show_project: bool) -> None:
        task = summary.task
        self._task_id = task.id
        done = task.status == TaskStatus.DONE
        self._binding = True
        try:
            self._check.set_active(done)
        finally:
            self._binding = False
        self._check.set_tooltip_text(_("Reopen") if done else _("Mark Done"))
        self._check.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [
                (_("Reopen “{title}”") if done else _("Mark “{title}” done")).format(
                    title=task.title
                )
            ],
        )
        if done:
            self.add_css_class("done")
        else:
            self.remove_css_class("done")

        set_dot_color(self._dot, summary.project_color)
        self._ticket.set_visible(bool(task.ticket_key))
        self._ticket.set_text(task.ticket_key or "")
        self._title.set_text(task.title)

        self._meta.remove_all()
        if show_project:
            self._meta.append(
                tip_if_trimmed(
                    Gtk.Label(
                        label=summary.project_name,
                        css_classes=["dim-label", "caption"],
                        ellipsize=Pango.EllipsizeMode.END,
                        use_markup=False,
                    )
                )
            )
        chips = task_chips(summary, today, tz)
        for chip in chips:
            self._meta.append(chip_widget(chip))
        self._meta.set_visible(show_project or bool(chips))

        parts = [task.title, summary.project_name]
        if task.ticket_key:
            parts.insert(0, task.ticket_key)
        parts += [chip.description for chip in chips]
        self.update_property([Gtk.AccessibleProperty.LABEL], [", ".join(parts)])

    @safe_callback
    def _on_toggled(self, check: Gtk.CheckButton) -> None:
        if self._binding or self._task_id is None:
            return
        self._on_done_toggled(self._task_id, check.get_active())
