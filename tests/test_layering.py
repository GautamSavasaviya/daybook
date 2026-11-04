"""Architecture rules from CLAUDE.md, checked on the source tree."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "daybook"


def modules(subdir: str = "") -> list[Path]:
    return sorted((PACKAGE / subdir).rglob("*.py"))


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


@pytest.mark.parametrize("path", modules("core"), ids=rel)
def test_core_never_imports_gi(path):
    bad = {n for n in imported_names(path) if n == "gi" or n.startswith("gi.")}
    assert not bad, f"{rel(path)} imports {bad}"


@pytest.mark.parametrize("path", modules("services"), ids=rel)
def test_services_never_import_widgets(path):
    banned = ("gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.Adw")
    bad = {n for n in imported_names(path) if n.startswith(banned)}
    assert not bad, f"{rel(path)} imports {bad}"


@pytest.mark.parametrize("path", modules("ui"), ids=rel)
def test_ui_never_touches_the_database(path):
    banned = ("sqlite3", "daybook.core.db", "daybook.core.repo", "daybook.core.migrations")
    # Whole modules and their submodules: `daybook.core.reports` is not `daybook.core.repo`.
    bad = {n for n in imported_names(path) if any(n == b or n.startswith(f"{b}.") for b in banned)}
    assert not bad, f"{rel(path)} imports {bad}"


CLOCK_CALLS = {("datetime", "now"), ("datetime", "utcnow"), ("date", "today"), ("time", "time")}


@pytest.mark.parametrize("path", modules(), ids=rel)
def test_only_the_clock_module_reads_the_time(path):
    if path == PACKAGE / "core" / "clock.py":
        return
    calls = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in CLOCK_CALLS
        ):
            calls.append(f"{node.func.value.id}.{node.func.attr} (line {node.lineno})")
    assert not calls, f"{rel(path)} reads the time directly: {calls}; inject a Clock"
