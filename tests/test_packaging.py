"""Packaging files stay consistent with daybook/const.py (the single APP_ID, D-005)."""

import configparser
import re
from pathlib import Path

from daybook.const import APP_ID, VERSION

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def test_app_id_and_version_are_well_formed():
    assert re.fullmatch(r"[a-z]+(\.[a-z][a-z0-9]*)+\.[A-Z][A-Za-z0-9]*", APP_ID)
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)


def test_app_id_is_written_only_in_const_py():
    offenders = [
        str(path.relative_to(ROOT))
        for path in [
            *ROOT.glob("daybook/**/*.py"),
            *DATA.rglob("*"),
            ROOT / "meson.build",
            *ROOT.glob("scripts/*"),
            *ROOT.glob("bin/*"),
        ]
        if path.is_file()
        and path.name != "const.py"
        and APP_ID in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []


def test_desktop_file_enables_dbus_activation():
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str  # keep key case
    entry.read(DATA / "daybook.desktop.in", encoding="utf-8")
    section = entry["Desktop Entry"]
    assert section["DBusActivatable"] == "true"
    assert section["Icon"] == "@APP_ID@"
    assert section["X-GNOME-UsesNotifications"] == "true"


def test_dbus_service_runs_the_gapplication_service():
    text = (DATA / "daybook.service.in").read_text(encoding="utf-8")
    assert "Name=@APP_ID@" in text
    assert "--gapplication-service" in text


def test_icons_exist():
    for name in ("app.svg", "app-symbolic.svg", "actions/daybook-inbox-symbolic.svg",
                 "actions/daybook-reports-symbolic.svg"):  # fmt: skip
        assert (DATA / "icons" / name).read_text(encoding="utf-8").startswith("<svg")


def test_devel_entry_is_hidden_and_not_dbus_activated():
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str
    entry.read(DATA / "daybook-devel.desktop.in", encoding="utf-8")
    section = entry["Desktop Entry"]
    assert section["NoDisplay"] == "true"
    assert section["X-GNOME-UsesNotifications"] == "true"
    # A notification click must never D-Bus-start a test instance without its variables.
    assert "DBusActivatable" not in section
