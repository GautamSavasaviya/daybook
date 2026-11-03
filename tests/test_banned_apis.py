"""No deprecated GTK/libadwaita APIs (docs/PLAN.md §1)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

GTK_BANNED = (
    r"TreeView\w*|TreeModel\w*|TreeStore|ListStore|TreeSelection|CellRenderer\w*|CellArea\w*"
    r"|ComboBox\w*|Dialog|MessageDialog|InfoBar|Statusbar|Assistant\w*|EntryCompletion"
    r"|FileChooser\w*|ColorChooser\w*|ColorButton|FontChooser\w*|FontButton|AppChooser\w*"
    r"|LockButton|VolumeButton|ShortcutsWindow|ShortcutsSection|ShortcutsGroup|ShortcutsShortcut"
    r"|show_uri\w*"
)
BANNED = re.compile(
    rf"\bGtk\.(?:{GTK_BANNED})\b"
    r"|\bGtk\.StyleContext\b(?!\.add_provider_for_display)"
    r"|\.get_style_context\("
    r"|\bAdw\.(?:MessageDialog|PreferencesWindow|Leaflet\w*|Flap\w*|Squeezer\w*"
    r"|ViewSwitcherTitle|AboutWindow)\b"
)


@pytest.mark.parametrize(
    "path", sorted((ROOT / "daybook").rglob("*.py")), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_deprecated_widgets(path):
    hits = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if BANNED.search(line)
    ]
    assert not hits, f"deprecated API in {path.name}: {hits}"


@pytest.mark.parametrize(
    "snippet",
    ["Gtk.TreeView()", "Gtk.Dialog(", "Gtk.show_uri(None, url, 0)", "Adw.MessageDialog(",
     "w.get_style_context()", "Gtk.StyleContext.add_class"],
)  # fmt: skip
def test_pattern_catches_banned_calls(snippet):
    assert BANNED.search(snippet)


@pytest.mark.parametrize(
    "snippet",
    ["Gtk.ListView()", "Adw.Dialog()", "Adw.AlertDialog(", "Gtk.FileDialog()",
     "Gtk.StyleContext.add_provider_for_display(d, p, 600)", "Gio.ListStore()"],
)  # fmt: skip
def test_pattern_allows_current_apis(snippet):
    assert not BANNED.search(snippet)
