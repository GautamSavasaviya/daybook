"""Standup (SPEC §6): Yesterday / Today / Blockers, one click from the clipboard."""

from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from daybook.core import standup
from daybook.core.standup import Standup
from daybook.services.context import AppContext
from daybook.services.events import Changed, DayChanged, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.export import copy_text


class StandupPane(Gtk.Box):
    def __init__(self, context: AppContext) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._ctx = context
        self._standup: Standup | None = None
        self._groups: list[Adw.PreferencesGroup] = []
        self._queued = False
        self._subscriptions: list[Subscription] = []

        actions = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        text = Gtk.Button(label=_("Copy as _Text"), use_underline=True)
        text.connect("clicked", self._on_copy_text)
        markdown = Gtk.Button(label=_("Copy as _Markdown"), use_underline=True)
        markdown.connect("clicked", self._on_copy_markdown)
        actions.append(text)
        actions.append(markdown)
        self.append(actions)
        self._page = Adw.PreferencesPage(vexpand=True)
        self.append(self._page)

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
        self._standup = result = self._ctx.reports.standup()
        for group in self._groups:
            self._page.remove(group)
        self._groups.clear()
        for heading, items in result.sections():
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(heading))
            for item in items:
                row = Adw.ActionRow(title=item, use_markup=False, title_lines=0, activatable=False)
                group.add(row)
            self._page.add(group)
            self._groups.append(group)
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_copy_text(self, _button: Gtk.Button) -> None:
        if self._standup is not None:
            copy_text(self, self._ctx, standup.to_text(self._standup), _("Standup"))

    @safe_callback
    def _on_copy_markdown(self, _button: Gtk.Button) -> None:
        if self._standup is not None:
            copy_text(self, self._ctx, standup.to_markdown(self._standup), _("Standup (Markdown)"))

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.TASK, Entity.PLAN, Entity.TIME_ENTRY, Entity.TIMER):
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
