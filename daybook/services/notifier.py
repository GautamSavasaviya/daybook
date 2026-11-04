"""Delivery: GNOME notifications with actions, sound, and the in-app reminder card.

Tier → priority and sound (D-053). One notification per subject: the id is the subject,
so a newer reminder replaces the banner and a moot one is withdrawn (D-054). Buttons are
`app.reminder((key, action))` actions; they reach the app over D-Bus, which needs the
installed .desktop and D-Bus service files (D-038).
"""

import logging
from gettext import gettext as _
from pathlib import Path
from typing import Protocol

from gi.repository import Gio, GLib

from daybook.core.reminders import SILENT_KINDS, SOUNDS, Action, Message, Reminder, Tier
from daybook.services.events import EventBus, ReminderGone, ReminderShown
from daybook.services.settings import SettingsService

log = logging.getLogger(__name__)

ACTION = "reminder"  # app.reminder, target (key, action)

_PRIORITIES = {
    Tier.STAY: Gio.NotificationPriority.URGENT,
    Tier.ACT: Gio.NotificationPriority.HIGH,
    Tier.INFO: Gio.NotificationPriority.NORMAL,
}
_NOTIFICATIONS_SCHEMA = "org.gnome.desktop.notifications"


class _Sounds(Protocol):
    def play_simple(self, attrs: dict[str, str], cancellable: None) -> object: ...


def _load_gsound() -> _Sounds | None:
    try:
        import gi  # noqa: PLC0415 — optional library, loaded only when a sound is played

        gi.require_version("GSound", "1.0")
        from gi.repository import GSound  # noqa: PLC0415

        context = GSound.Context()
        context.init()
    except (ImportError, ValueError, GLib.Error) as exc:
        log.debug("GSound is not available, so reminders are silent: %s", exc)
        return None
    sounds: _Sounds = context
    return sounds


class Notifier:
    def __init__(self, bus: EventBus, settings: SettingsService) -> None:
        self._bus = bus
        self._settings = settings
        self._app: Gio.Application | None = None
        self._sound: _Sounds | None = None
        self._sound_loaded = False
        self._banners: Gio.Settings | None = None
        # The latest live reminder per subject, for windows opened after it fired.
        self.live: dict[str, ReminderShown] = {}
        source = Gio.SettingsSchemaSource.get_default()
        if source is not None and source.lookup(_NOTIFICATIONS_SCHEMA, True) is not None:
            self._banners = Gio.Settings.new(_NOTIFICATIONS_SCHEMA)

    def attach(self, application: Gio.Application) -> None:
        self._app = application

    def show(self, reminder: Reminder, message: Message) -> None:
        if reminder.tier == Tier.SILENT:
            return
        event = ReminderShown(reminder.key, reminder.subject, reminder.tier, message)
        self.live.pop(reminder.subject, None)  # re-insert: the dict stays oldest → newest
        self.live[reminder.subject] = event
        self._bus.publish(event)
        if self._app is not None:
            self._app.send_notification(reminder.subject, self._notification(reminder, message))
        self._play(reminder)
        log.info("reminder %s: %s", reminder.key, message.title)

    def withdraw(self, subject: str) -> None:
        self.live.pop(subject, None)
        if self._app is not None:
            self._app.withdraw_notification(subject)
        self._bus.publish(ReminderGone(subject))

    def _notification(self, reminder: Reminder, message: Message) -> Gio.Notification:
        notification = Gio.Notification.new(message.title)
        if message.body:
            notification.set_body(message.body)
        notification.set_priority(_PRIORITIES[reminder.tier])
        for button in message.buttons[:3]:  # GNOME shows three (D-054)
            notification.add_button_with_target(
                button.label, f"app.{ACTION}", GLib.Variant("(ss)", (reminder.key, button.action))
            )
        notification.set_default_action_and_target(
            f"app.{ACTION}", GLib.Variant("(ss)", (reminder.key, Action.OPEN.value))
        )
        return notification

    def _play(self, reminder: Reminder) -> None:
        if not self._settings.current.sound_enabled or reminder.kind in SILENT_KINDS:
            return
        # Do Not Disturb hides banners; only urgent reminders still make a sound (D-053).
        banners = self._banners is None or self._banners.get_boolean("show-banners")
        if reminder.tier != Tier.STAY and not banners:
            return
        if not self._sound_loaded:
            self._sound = _load_gsound()
            self._sound_loaded = True
        if self._sound is None:
            return
        try:
            self._sound.play_simple({"event.id": SOUNDS[reminder.tier]}, None)
        except GLib.Error as exc:
            log.debug("could not play %s: %s", SOUNDS[reminder.tier], exc)


# Diagnostics --------------------------------------------------------------------------------


def _data_dirs() -> list[Path]:
    return [Path(GLib.get_user_data_dir()), *(Path(d) for d in GLib.get_system_data_dirs())]


def diagnose(app_id: str, devel: bool) -> list[str]:
    """What stops notification buttons from working on this machine, in plain words.
    Empty when everything is in place (Preferences → Notifications)."""
    problems: list[str] = []
    dirs = _data_dirs()
    if not any((d / "applications" / f"{app_id}.desktop").exists() for d in dirs):
        problems.append(
            _(
                "Daybook is not installed, so GNOME can't show its notifications. "
                "Run scripts/dev-install.sh."
            )
        )
    elif not devel and not any(
        (d / "dbus-1" / "services" / f"{app_id}.service").exists() for d in dirs
    ):
        problems.append(
            _(
                "The D-Bus service file is missing, so notification buttons can't start "
                "Daybook when it isn't running. Run scripts/dev-install.sh."
            )
        )
    if not _notification_server():
        problems.append(_("No notification service is running in this session."))
    return problems


def _notification_server() -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.gtk.Notifications",)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            500,
            None,
        )
    except GLib.Error as exc:
        log.info("could not ask the session bus about notifications: %s", exc)
        return False
    return bool(reply.unpack()[0])
