"""The tray icon (D-032): a StatusNotifierItem with a `com.canonical.dbusmenu` menu, spoken
directly over D-Bus, with no GTK 3 library. Ubuntu's AppIndicator extension shows it.

It registers with `org.kde.StatusNotifierWatcher` whenever one is there, and does nothing,
silently, when there isn't (or when the host goes away). The words and the menu are
`core/traymenu.py`; this file only encodes them for D-Bus.
"""

import logging
import os
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from daybook.const import APP_ID
from daybook.core import traymenu
from daybook.core.traymenu import Command, Entry, TrayStatus
from daybook.services.events import Changed, Entity, EventBus
from daybook.services.safe import report_error

log = logging.getLogger(__name__)

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
TICK_SECONDS = 30

_ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/>
    </method>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/><arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/><arg type="u" name="timestamp"/>
    </signal>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
  </interface>
</node>
"""


def _props(entry: Entry) -> dict[str, GLib.Variant]:
    if entry.command is None:
        return {"type": GLib.Variant("s", "separator")}
    return {
        "label": GLib.Variant("s", entry.label.replace("_", "__")),
        "enabled": GLib.Variant("b", entry.enabled),
        "visible": GLib.Variant("b", True),
    }


def _wanted(props: dict[str, GLib.Variant], names: list[str]) -> dict[str, GLib.Variant]:
    return {k: v for k, v in props.items() if not names or k in names}


class TrayService:
    def __init__(
        self,
        bus: EventBus,
        *,
        status: Callable[[], TrayStatus],
        on_command: Callable[[Command], None],
        icon_name: str = APP_ID,
    ) -> None:
        self._bus = bus
        self._status_of = status
        self._on_command = on_command
        self._icon = icon_name
        self._status = status()
        self._revision = 1
        self._conn: Gio.DBusConnection | None = None
        self._registrations: list[int] = []
        self._owner_id = 0
        self._watch_id = 0
        self._tick_id: int | None = None
        self.registered = False
        self._subscription = bus.subscribe(Changed, self._on_changed)

    # Lifecycle --------------------------------------------------------------------------

    def start(self, connection: Gio.DBusConnection) -> None:
        self._conn = connection
        item = Gio.DBusNodeInfo.new_for_xml(_ITEM_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
        self._registrations = [
            connection.register_object_with_closures2(
                ITEM_PATH, item, self._item_call, self._item_property, None
            ),
            connection.register_object_with_closures2(
                MENU_PATH, menu, self._menu_call, self._menu_property, None
            ),
        ]
        self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._owner_id = Gio.bus_own_name_on_connection(
            connection, self._name, Gio.BusNameOwnerFlags.NONE, None, None
        )
        # Watching the watcher registers us when it appears, again if it restarts, and does
        # nothing at all when no desktop shows tray icons (D-032).
        self._watch_id = Gio.bus_watch_name_on_connection(
            connection,
            WATCHER,
            Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared,
            self._on_watcher_vanished,
        )
        self._tick_id = GLib.timeout_add_seconds(TICK_SECONDS, self._on_tick)

    def stop(self) -> None:
        self._subscription.unsubscribe()
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        if self._conn is not None:
            for registration in self._registrations:
                self._conn.unregister_object(registration)
        self._registrations = []
        self.registered = False

    def _on_watcher_appeared(self, connection: Gio.DBusConnection, _name: str, _owner: str) -> None:
        connection.call(
            WATCHER,
            WATCHER_PATH,
            WATCHER,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._name,)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            self._on_registered,
        )

    def _on_registered(self, connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error as exc:
            log.info("no tray host accepted the icon: %s", exc.message)
            return
        self.registered = True
        log.info("tray icon registered")

    def _on_watcher_vanished(self, _connection: Gio.DBusConnection, _name: str) -> None:
        if self.registered:
            log.info("the tray host went away")
        self.registered = False

    # Keeping the text fresh --------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the timer and tell the host what changed."""
        new = self._status_of()
        old, self._status = self._status, new
        if self._conn is None or new == old:
            return
        if traymenu.title(new) != traymenu.title(old):
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewTitle", None)
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
        if traymenu.menu(new) != traymenu.menu(old):
            self._revision += 1
            self._emit(
                MENU_PATH,
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self._revision, 0)),
            )

    def _emit(self, path: str, interface: str, signal: str, params: GLib.Variant | None) -> None:
        assert self._conn is not None
        try:
            self._conn.emit_signal(None, path, interface, signal, params)
        except GLib.Error:
            log.debug("could not emit %s", signal, exc_info=True)

    def _on_changed(self, event: Changed) -> None:
        if event.entity in (Entity.TIMER, Entity.TIME_ENTRY, Entity.TASK, Entity.PLAN):
            self.refresh()

    def _on_tick(self) -> bool:
        # Not @safe_callback: a None result on failure would stop the ticking for good.
        try:
            self.refresh()
        except Exception as exc:
            report_error(exc)
        return GLib.SOURCE_CONTINUE

    # The item ---------------------------------------------------------------------------

    def _item_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant:
        status = self._status
        values: dict[str, GLib.Variant] = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", traymenu.title(status)),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self._icon),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant(
                "(sa(iiay)ss)", (self._icon, [], traymenu.title(status), traymenu.tooltip(status))
            ),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name, GLib.Variant("s", ""))

    def _item_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        _parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        try:
            if method in ("Activate", "SecondaryActivate"):
                self._on_command(Command.SHOW)
            # ContextMenu and Scroll need nothing: the host draws the menu from /MenuBar.
        except Exception as exc:
            report_error(exc)
        invocation.return_value(None)

    # The menu ---------------------------------------------------------------------------

    def _menu_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant:
        values: dict[str, GLib.Variant] = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }
        return values.get(name, GLib.Variant("s", ""))

    def _menu_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        try:
            reply = self.handle_menu_call(method, parameters.unpack())
        except Exception as exc:
            report_error(exc)
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(exc))
            return
        invocation.return_value(reply)

    def handle_menu_call(self, method: str, args: tuple[Any, ...]) -> GLib.Variant | None:
        """The dbusmenu methods, on plain values (so they can be tested without a bus)."""
        handlers: dict[str, Callable[[tuple[Any, ...]], GLib.Variant | None]] = {
            "GetLayout": self._get_layout,
            "GetGroupProperties": self._get_group_properties,
            "GetProperty": self._get_property,
            "Event": self._event,
            "EventGroup": self._event_group,
            "AboutToShow": lambda _args: GLib.Variant("(b)", (False,)),
            "AboutToShowGroup": lambda _args: GLib.Variant("(aiai)", ([], [])),
        }
        if method not in handlers:
            raise ValueError(f"unknown method {method}")
        return handlers[method](args)

    def _get_layout(self, _args: tuple[Any, ...]) -> GLib.Variant:
        entries = traymenu.menu(self._status)
        return GLib.Variant("(u(ia{sv}av))", (self._revision, _layout_tuple(entries)))

    def _get_group_properties(self, args: tuple[Any, ...]) -> GLib.Variant:
        ids, names = args
        entries = traymenu.menu(self._status)
        found = [(e.id, _wanted(_props(e), names)) for e in entries if not ids or e.id in ids]
        return GLib.Variant("(a(ia{sv}))", (found,))

    def _get_property(self, args: tuple[Any, ...]) -> GLib.Variant:
        entry_id, name = args
        entry = next((e for e in traymenu.menu(self._status) if e.id == entry_id), None)
        value = _props(entry).get(name) if entry is not None else None
        return GLib.Variant("(v)", (value if value is not None else GLib.Variant("s", ""),))

    def _event(self, args: tuple[Any, ...]) -> None:
        entry_id, event, _data, _stamp = args
        self._clicked(entry_id, event)

    def _event_group(self, args: tuple[Any, ...]) -> GLib.Variant:
        (events,) = args
        for entry_id, event, _data, _stamp in events:
            self._clicked(entry_id, event)
        return GLib.Variant("(ai)", ([],))

    def _clicked(self, entry_id: int, event: str) -> None:
        if event != "clicked":
            return
        command = traymenu.command_for(entry_id, self._status)
        if command is not None:
            self._on_command(command)


def _layout_tuple(entries: tuple[Entry, ...]) -> tuple[int, dict[str, GLib.Variant], list[Any]]:
    children = [GLib.Variant("(ia{sv}av)", (e.id, _props(e), [])) for e in entries]
    return (0, {"children-display": GLib.Variant("s", "submenu")}, children)
