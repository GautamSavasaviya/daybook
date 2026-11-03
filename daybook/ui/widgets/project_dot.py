"""The colour dot for projects and areas (PLAN §6.2). Never the only signal: the project
name is always shown next to it or in the accessible label (PLAN §6.4)."""

from gettext import gettext as _

from gi.repository import Adw, Gdk, GObject, Graphene, Gsk, Gtk

from daybook.core.models import ProjectColor

_ACCENTS = {
    ProjectColor.BLUE: Adw.AccentColor.BLUE,
    ProjectColor.TEAL: Adw.AccentColor.TEAL,
    ProjectColor.GREEN: Adw.AccentColor.GREEN,
    ProjectColor.YELLOW: Adw.AccentColor.YELLOW,
    ProjectColor.ORANGE: Adw.AccentColor.ORANGE,
    ProjectColor.RED: Adw.AccentColor.RED,
    ProjectColor.PINK: Adw.AccentColor.PINK,
    ProjectColor.PURPLE: Adw.AccentColor.PURPLE,
    ProjectColor.SLATE: Adw.AccentColor.SLATE,
}


def color_name(color: ProjectColor) -> str:
    """The palette colour's name, for accessible labels and the colour picker."""
    names = {
        ProjectColor.BLUE: _("Blue"),
        ProjectColor.TEAL: _("Teal"),
        ProjectColor.GREEN: _("Green"),
        ProjectColor.YELLOW: _("Yellow"),
        ProjectColor.ORANGE: _("Orange"),
        ProjectColor.RED: _("Red"),
        ProjectColor.PINK: _("Pink"),
        ProjectColor.PURPLE: _("Purple"),
        ProjectColor.SLATE: _("Slate"),
    }
    return names[color]


def dot(color: ProjectColor) -> Gtk.Widget:
    widget = Gtk.Box(
        valign=Gtk.Align.CENTER,
        halign=Gtk.Align.CENTER,
        accessible_role=Gtk.AccessibleRole.PRESENTATION,
    )
    set_dot_color(widget, color)
    return widget


def set_dot_color(widget: Gtk.Widget, color: ProjectColor) -> None:
    widget.set_css_classes(["project-dot", color.value])


class DotPaintable(GObject.Object, Gdk.Paintable):
    """A dot for places that take an icon paintable (the Adw.Sidebar item icon)."""

    def __init__(self, color: ProjectColor) -> None:
        super().__init__()
        self._rgba = Adw.AccentColor.to_rgba(_ACCENTS[color])

    def do_snapshot(self, snapshot: Gdk.Snapshot, width: float, height: float) -> None:
        size = min(width, height) * 0.625
        rect = Graphene.Rect().init((width - size) / 2, (height - size) / 2, size, size)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(rect, size / 2)
        assert isinstance(snapshot, Gtk.Snapshot)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(self._rgba, rect)
        snapshot.pop()

    def do_get_intrinsic_width(self) -> int:
        return 16

    def do_get_intrinsic_height(self) -> int:
        return 16

    def do_get_flags(self) -> Gdk.PaintableFlags:
        # The stubs call these SIZE and CONTENTS; PyGObject exposes the GIR names.
        static = Gdk.PaintableFlags.STATIC_SIZE  # type: ignore[attr-defined]
        contents = Gdk.PaintableFlags.STATIC_CONTENTS  # type: ignore[attr-defined]
        flags: Gdk.PaintableFlags = static | contents
        return flags
