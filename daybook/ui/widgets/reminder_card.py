"""The in-app reminder card: the live reminder with the same buttons as its notification.

The main window shows it above the page, and the Focus window inside its reminder slot
(D-055). Adw.Banner has room for one button only, so this is a small card (D-098).
"""

from collections.abc import Callable, Mapping
from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango

from daybook.core.reminders import Action, Tier
from daybook.services.events import ReminderShown
from daybook.services.safe import safe_callback

OnAction = Callable[[str, str], None]  # (reminder key, action)


_URGENCY = (Tier.STAY, Tier.ACT, Tier.INFO)


def most_urgent(live: Mapping[str, ReminderShown]) -> ReminderShown | None:
    """The reminder the card shows: the most urgent tier first, then the newest, so a
    later "10 min left" never hides a meeting that is about to start."""
    ranked = sorted(
        enumerate(live.values()), key=lambda pair: (_URGENCY.index(pair[1].tier), -pair[0])
    )
    return ranked[0][1] if ranked else None


class ReminderCard(Gtk.Revealer):
    def __init__(self, on_action: OnAction) -> None:
        super().__init__(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=200
        )
        self._on_action = on_action
        self._event: ReminderShown | None = None

        self._card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            css_classes=["card", "reminder-card"],
            accessible_role=Gtk.AccessibleRole.ALERT,
        )
        top = Gtk.Box(spacing=6)
        self._title = Gtk.Label(
            xalign=0,
            hexpand=True,
            wrap=True,
            css_classes=["heading"],
            use_markup=False,
        )
        close = Gtk.Button(
            icon_name="window-close-symbolic",
            css_classes=["flat", "circular"],
            valign=Gtk.Align.START,
            tooltip_text=_("Dismiss"),
        )
        close.update_property([Gtk.AccessibleProperty.LABEL], [_("Dismiss reminder")])
        close.connect("clicked", self._on_dismiss)
        top.append(self._title)
        top.append(close)
        self._body = Gtk.Label(
            xalign=0, wrap=True, use_markup=False, wrap_mode=Pango.WrapMode.WORD_CHAR
        )
        self._buttons = Adw.WrapBox(child_spacing=6, line_spacing=6)
        self._card.append(top)
        self._card.append(self._body)
        self._card.append(self._buttons)
        self.set_child(self._card)

    @property
    def subject(self) -> str | None:
        return self._event.subject if self._event is not None else None

    def show_reminder(self, event: ReminderShown) -> None:
        self._event = event
        message = event.message
        self._title.set_text(message.title)
        self._body.set_text(message.body)
        self._body.set_visible(bool(message.body))
        self._buttons.remove_all()
        for button in message.buttons:
            widget = Gtk.Button(label=button.label)
            widget.connect("clicked", self._on_button, button.action)
            self._buttons.append(widget)
        self._buttons.set_visible(bool(message.buttons))
        for tier in Tier:
            self._card.remove_css_class(tier.value)
        self._card.add_css_class(event.tier.value)
        self.set_reveal_child(True)

    def hide_reminder(self) -> None:
        self._event = None
        self.set_reveal_child(False)

    def show_most_urgent(self, live: Mapping[str, ReminderShown]) -> None:
        event = most_urgent(live)
        if event is None:
            self.hide_reminder()
        elif self._event is None or event.key != self._event.key:
            self.show_reminder(event)

    @safe_callback
    def _on_button(self, _button: Gtk.Button, action: Action) -> None:
        if self._event is not None:
            key = self._event.key
            self.hide_reminder()
            self._on_action(key, action.value)

    @safe_callback
    def _on_dismiss(self, _button: Gtk.Button) -> None:
        if self._event is not None:
            key = self._event.key
            self.hide_reminder()
            self._on_action(key, Action.DISMISSED.value)
