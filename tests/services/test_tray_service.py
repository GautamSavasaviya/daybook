"""The tray over a private D-Bus: a fake tray host, and a client that calls the menu the way
GNOME's AppIndicator does. Nothing here touches the real desktop."""

import subprocess
import time

import pytest
from gi.repository import Gio, GLib

from daybook.core.traymenu import Command, TrayStatus
from daybook.services.events import Changed, Entity, EventBus
from daybook.services.tray import ITEM_PATH, MENU_PATH, WATCHER, WATCHER_PATH, TrayService

ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" name="service" direction="in"/></method>
</interface></node>
"""

RUNNING = TrayStatus("PROJ-1 Fix login", True, 23)
IDLE = TrayStatus(None, False, None)


def pump(until, timeout=5.0):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)
    assert until(), "timed out waiting on the bus"


@pytest.fixture
def desktop():
    """A private session bus. It is started by hand and addressed explicitly: Gio.TestDBus
    would change the process's DBUS_SESSION_BUS_ADDRESS, and GSettings (dconf) then holds a
    connection to it, so the daemon would take 30 s to stop."""
    daemon = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert daemon.stdout is not None
    address = daemon.stdout.readline().strip()
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )
    connections = []

    def new_connection():
        connections.append(Gio.DBusConnection.new_for_address_sync(address, flags, None, None))
        return connections[-1]

    yield new_connection
    for conn in connections:
        conn.close_sync(None)
    daemon.terminate()
    daemon.wait(timeout=10)
    daemon.stdout.close()


class FakeHost:
    """Owns org.kde.StatusNotifierWatcher and records what registers."""

    def __init__(self, connection):
        self.registered = []
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        connection.register_object_with_closures2(WATCHER_PATH, info, self._call, None, None)
        self.owner = Gio.bus_own_name_on_connection(
            connection, WATCHER, Gio.BusNameOwnerFlags.NONE, None, None
        )
        self.connection = connection

    def _call(self, _c, _sender, _path, _iface, _method, params, invocation):
        self.registered.append(params.unpack()[0])
        invocation.return_value(None)

    def leave(self):
        Gio.bus_unown_name(self.owner)


class Client:
    def __init__(self, connection, service):
        self.connection = connection
        self.service = service

    def call(self, path, iface, method, args=None, reply=None):
        out = []
        self.connection.call(
            self.service,
            path,
            iface,
            method,
            args,
            reply,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
            lambda c, r: out.append(c.call_finish(r)),
        )
        pump(lambda: out)
        return out[0]

    def prop(self, path, iface, name):
        result = self.call(
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (iface, name)),
        )
        return result.unpack()[0]


def start(desktop, status, commands):
    bus = EventBus(lambda cb: cb())
    current = {"status": status}
    tray = TrayService(bus, status=lambda: current["status"], on_command=commands.append)
    tray.start(desktop())
    return tray, bus, current


def test_registers_with_the_host_and_serves_the_item(desktop):
    host = FakeHost(desktop())
    commands = []
    tray, _bus, _current = start(desktop, RUNNING, commands)
    pump(lambda: tray.registered)
    assert len(host.registered) == 1
    service = host.registered[0]
    assert service.startswith("org.kde.StatusNotifierItem-")

    client = Client(desktop(), service)
    assert client.prop(ITEM_PATH, ITEM_IFACE, "Id") == "io.github.daybook.Daybook"
    assert client.prop(ITEM_PATH, ITEM_IFACE, "Title") == "PROJ-1 Fix login"
    assert client.prop(ITEM_PATH, ITEM_IFACE, "Status") == "Active"
    assert client.prop(ITEM_PATH, ITEM_IFACE, "Menu") == MENU_PATH
    tip = client.prop(ITEM_PATH, ITEM_IFACE, "ToolTip")
    assert tip[2:] == ("PROJ-1 Fix login", "PROJ-1 Fix login · 23m left · running")

    client.call(ITEM_PATH, ITEM_IFACE, "Activate", GLib.Variant("(ii)", (0, 0)))
    assert commands == [Command.SHOW]
    tray.stop()


def test_the_menu_layout_and_clicks(desktop):
    FakeHost(desktop())
    commands = []
    tray, _bus, current = start(desktop, RUNNING, commands)
    pump(lambda: tray.registered)
    client = Client(desktop(), tray._name)

    revision, root = client.call(
        MENU_PATH,
        MENU_IFACE,
        "GetLayout",
        GLib.Variant("(iias)", (0, -1, [])),
    ).unpack()
    assert revision == 1
    root_id, root_props, children = root
    assert root_id == 0
    assert root_props["children-display"] == "submenu"
    labels = [(c[0], c[1].get("label"), c[1].get("type")) for c in children]
    assert labels == [
        (1, "Show Daybook", None),
        (2, "Quick Add…", None),
        (3, "Meeting Now…", None),
        (4, "Pause Timer", None),
        (5, None, "separator"),
        (6, "Quit", None),
    ]
    assert client.prop(MENU_PATH, MENU_IFACE, "Version") == 3

    for entry_id in (1, 2, 3, 4, 6):
        client.call(
            MENU_PATH,
            MENU_IFACE,
            "Event",
            GLib.Variant("(isvu)", (entry_id, "clicked", GLib.Variant("s", ""), 0)),
        )
    assert commands == [
        Command.SHOW,
        Command.QUICK_ADD,
        Command.MEETING_NOW,
        Command.TOGGLE_TIMER,
        Command.QUIT,
    ]
    commands.clear()
    # Hovering and the like are not clicks.
    client.call(
        MENU_PATH,
        MENU_IFACE,
        "Event",
        GLib.Variant("(isvu)", (1, "hovered", GLib.Variant("s", ""), 0)),
    )
    # Several at once, and a disabled entry does nothing.
    current["status"] = IDLE
    tray.refresh()
    client.call(
        MENU_PATH,
        MENU_IFACE,
        "EventGroup",
        GLib.Variant(
            "(a(isvu))",
            ([(4, "clicked", GLib.Variant("s", ""), 0), (2, "clicked", GLib.Variant("s", ""), 0)],),
        ),
    )
    assert commands == [Command.QUICK_ADD]
    tray.stop()


def test_the_other_menu_methods(desktop):
    FakeHost(desktop())
    tray, _bus, _current = start(desktop, RUNNING, [])
    pump(lambda: tray.registered)
    client = Client(desktop(), tray._name)
    (props,) = client.call(
        MENU_PATH, MENU_IFACE, "GetGroupProperties", GLib.Variant("(aias)", ([1, 4], ["label"]))
    ).unpack()
    assert props == [(1, {"label": "Show Daybook"}), (4, {"label": "Pause Timer"})]
    (value,) = client.call(
        MENU_PATH, MENU_IFACE, "GetProperty", GLib.Variant("(is)", (6, "label"))
    ).unpack()
    assert value == "Quit"
    (missing,) = client.call(
        MENU_PATH, MENU_IFACE, "GetProperty", GLib.Variant("(is)", (99, "label"))
    ).unpack()
    assert missing == ""
    assert client.call(
        MENU_PATH, MENU_IFACE, "AboutToShow", GLib.Variant("(i)", (0,))
    ).unpack() == (False,)
    assert client.call(
        MENU_PATH, MENU_IFACE, "AboutToShowGroup", GLib.Variant("(ai)", ([0],))
    ).unpack() == ([], [])
    with pytest.raises(ValueError, match="unknown method"):
        tray.handle_menu_call("Nope", ())
    tray.stop()


def test_changes_are_announced_and_a_missing_host_is_fine(desktop):
    commands = []
    tray, bus, current = start(desktop, IDLE, commands)  # no host on the bus at all
    client = Client(desktop(), tray._name)
    assert not tray.registered
    assert client.prop(ITEM_PATH, ITEM_IFACE, "Title") == "Daybook"

    seen = []
    watcher = desktop()
    watcher.signal_subscribe(
        None,
        None,
        None,
        None,
        None,
        Gio.DBusSignalFlags.NONE,
        lambda _c, _s, _p, iface, name, _params: seen.append((iface, name)),
    )
    current["status"] = RUNNING
    bus.publish(Changed(Entity.TIMER))
    pump(lambda: len(seen) >= 3)
    assert (ITEM_IFACE, "NewTitle") in seen
    assert (ITEM_IFACE, "NewToolTip") in seen
    assert (MENU_IFACE, "LayoutUpdated") in seen
    assert client.prop(ITEM_PATH, ITEM_IFACE, "Title") == "PROJ-1 Fix login"
    revision, _ = client.call(
        MENU_PATH, MENU_IFACE, "GetLayout", GLib.Variant("(iias)", (0, -1, []))
    ).unpack()
    assert revision == 2

    # A host that appears later gets the icon, and one that leaves is noticed.
    host = FakeHost(desktop())
    pump(lambda: tray.registered)
    assert host.registered == [tray._name]
    host.leave()
    pump(lambda: not tray.registered)
    tray.stop()
    tray.stop()  # stopping twice is fine


def test_tick_and_unrelated_changes(desktop):
    tray, bus, current = start(desktop, IDLE, [])
    seen = []
    watcher = desktop()
    watcher.signal_subscribe(
        None, None, None, None, None, Gio.DBusSignalFlags.NONE, lambda *a: seen.append(a[4])
    )
    bus.publish(Changed(Entity.PROJECT))  # not about the timer: nothing to say
    current["status"] = TrayStatus("X", False, None, 5)
    assert tray._on_tick() is True
    pump(lambda: "NewToolTip" in seen)
    tray.stop()
