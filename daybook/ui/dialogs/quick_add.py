"""Quick add (Ctrl+K, `daybook --quick-add`): one line, a live preview, Enter to add.

The line is parsed on every keystroke (`core/quickadd.py`); the preview shows what the
task will be, errors keep Add switched off, and `+name` completes from earlier names.
"""

from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk, Pango

from daybook.core import quickadd
from daybook.core.errors import ValidationError
from daybook.core.models import Project
from daybook.core.quickadd import ParseResult
from daybook.core.timeutil import fmt_day, fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import Toast
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.dialogs.sticky import StickyDialog
from daybook.ui.widgets.project_dot import dot
from daybook.ui.widgets.suggest import Suggestions

EXAMPLE = "PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today"


def _chip(text: str, *classes: str) -> Gtk.Widget:
    return Gtk.Label(label=text, css_classes=["chip", "caption", *classes], use_markup=False)


class QuickAddDialog(StickyDialog):
    def __init__(self, context: AppContext, text: str = "") -> None:
        super().__init__(title=_("Quick Add"), content_width=560)
        self._ctx = context
        self._result: ParseResult | None = None
        self._queued = False
        self._projects: dict[int, Project] = {p.id: p for p in context.projects.all()}

        self._entry = Adw.EntryRow(title=_("Task"), activates_default=False)
        self._entry.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION], [_("Example: {line}").format(line=EXAMPLE)]
        )
        self._entry.connect("changed", self._on_changed)
        self._entry.connect("entry-activated", self._on_activated)
        Suggestions(self._entry, self._names, insert=self._insert_name)
        line = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        line.append(self._entry)

        self._title = Gtk.Label(
            xalign=0, wrap=True, css_classes=["heading"], use_markup=False, visible=False
        )
        self._chips = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self._problems = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            accessible_role=Gtk.AccessibleRole.STATUS,
        )
        preview = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["card", "quick-preview"]
        )
        for part in (self._title, self._chips, self._problems):
            preview.append(part)

        hint = Gtk.Label(
            label=_(
                "#project or #tag · +requested_by · !p1–!p4 · ~estimate · ^due · "
                "@plan (=2h or =day)"
            ),
            xalign=0,
            wrap=True,
            css_classes=["caption", "dim-label"],
        )
        self._add = Gtk.Button(
            label=_("_Add"),
            use_underline=True,
            css_classes=["suggested-action"],
            sensitive=False,
            valign=Gtk.Align.CENTER,
        )
        self._add.connect("clicked", self._on_add_clicked)
        footer = Gtk.Box(spacing=12)
        hint.set_hexpand(True)
        footer.append(hint)
        footer.append(self._add)

        body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=6,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        for widget in (line, preview, footer):
            body.append(widget)
        view = Adw.ToolbarView(content=body)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)

        self._entry.set_text(text)
        self._refresh()
        self.set_focus(self._entry)
        # Focusing selects the text; text passed in (`--quick-add …`) is meant to be
        # continued, so on the first focus the cursor goes to its end instead.
        self._placed = False
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._on_focus_enter)
        self._entry.add_controller(focus)

    # Parsing ----------------------------------------------------------------------------

    def _refresh(self) -> None:
        text = self._entry.get_text()
        self._result = result = self._ctx.tasks.parse_quick(text) if text.strip() else None
        self._chips.remove_all()
        while (child := self._problems.get_first_child()) is not None:
            self._problems.remove(child)
        if result is None:
            self._title.set_visible(False)
            self._problems.append(
                Gtk.Label(
                    label=_("Type a task, for example: {line}").format(line=EXAMPLE),
                    xalign=0,
                    wrap=True,
                    css_classes=["dim-label"],
                    use_markup=False,
                )
            )
            self._add.set_sensitive(False)
            return
        parsed = result.parsed
        self._title.set_text(parsed.title or _("(no title yet)"))
        self._title.set_visible(True)
        for chip in self._preview_chips(result):
            self._chips.append(chip)
        for message, css in [(m, "field-error") for m in result.errors] + [
            (m, "warning") for m in result.warnings
        ]:
            label = Gtk.Label(
                label=message,
                xalign=0,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                css_classes=["caption", css],
                use_markup=False,
            )
            self._problems.append(label)
        self._add.set_sensitive(result.ok)

    def _preview_chips(self, result: ParseResult) -> list[Gtk.Widget]:
        parsed = result.parsed
        today = self._ctx.tasks.today()
        chips: list[Gtk.Widget] = []
        project = self._projects.get(parsed.project_id)
        if project is not None:
            box = Gtk.Box(spacing=6, css_classes=["chip", "caption"])
            box.append(dot(project.color))
            box.append(Gtk.Label(label=project.name, use_markup=False))
            chips.append(box)
        if parsed.ticket_key:
            chips.append(_chip(parsed.ticket_key))
        chips.append(_chip(f"P{parsed.priority.value}"))
        if parsed.estimate_min:
            chips.append(_chip(_("~{time}").format(time=fmt_duration(parsed.estimate_min))))
        if parsed.due_date:
            chips.append(_chip(_("due {day}").format(day=fmt_day(parsed.due_date, today))))
        if parsed.plan is not None:
            amount = (
                _("rest of day")
                if parsed.plan.allotted_min is None
                else fmt_duration(parsed.plan.allotted_min)
            )
            label = _("{day}: {amount}").format(
                day=fmt_day(parsed.plan.day, today).capitalize(), amount=amount
            )
            chips.append(_chip(label, "accent"))
        if parsed.requested_by:
            chips.append(_chip(_("from {name}").format(name=parsed.requested_by), "person"))
        chips.extend(_chip(f"#{tag}") for tag in parsed.tags)
        return chips

    # +name completion -------------------------------------------------------------------

    def _names(self, text: str) -> list[str]:
        _start, token = quickadd.token_at(text, self._entry.get_position())
        if not token.startswith("+") or len(token) < 2:
            return []
        typed = token[1:].replace("_", " ")
        names = self._ctx.tasks.requested_by_suggestions(typed)
        return ["+" + name.replace(" ", "_") for name in names if name != typed]

    def _insert_name(self, value: str) -> None:
        text = self._entry.get_text()
        start, token = quickadd.token_at(text, self._entry.get_position())
        new = f"{text[:start]}{value} {text[start + len(token) :].lstrip()}"
        self._entry.set_text(new)
        self._entry.set_position(start + len(value) + 1)

    # Adding -----------------------------------------------------------------------------

    def _submit(self) -> None:
        result = self._result
        if result is None or not result.ok:
            return
        try:
            self._ctx.tasks.quick_add(self._entry.get_text())
        except ValidationError as error:
            self._problems.append(
                Gtk.Label(
                    label=error.message,
                    xalign=0,
                    wrap=True,
                    css_classes=["caption", "field-error"],
                    use_markup=False,
                )
            )
            self._add.set_sensitive(False)
            return
        parsed = result.parsed
        title = short(parsed.title)
        if parsed.plan is not None:
            message = _("Added “{title}” to {day}").format(
                title=title, day=fmt_day(parsed.plan.day, self._ctx.tasks.today())
            )
        else:
            message = _("Added “{title}”").format(title=title)
        self._ctx.bus.publish(Toast(message))
        self.close()

    @safe_callback
    def _on_changed(self, _row: Adw.EntryRow) -> None:
        # Parse once per burst of typing (a paste changes the text many times).
        if not self._queued:
            self._queued = True
            GLib.idle_add(self._refresh_idle)

    @safe_callback
    def _refresh_idle(self) -> bool:
        self._queued = False
        self._refresh()
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_focus_enter(self, _controller: Gtk.EventControllerFocus) -> None:
        if not self._placed:
            self._placed = True
            GLib.idle_add(self._cursor_to_end)

    @safe_callback
    def _cursor_to_end(self) -> bool:
        end = len(self._entry.get_text())
        self._entry.select_region(end, end)
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_activated(self, _row: Adw.EntryRow) -> None:
        self._refresh()
        self._submit()

    @safe_callback
    def _on_add_clicked(self, _button: Gtk.Button) -> None:
        self._submit()
