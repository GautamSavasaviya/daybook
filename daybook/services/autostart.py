"""Start at login (SPEC §7): an autostart entry that runs `daybook --background`."""

import logging
from pathlib import Path

from daybook.const import APP_ID, APP_NAME

log = logging.getLogger(__name__)


def entry_text(exec_path: str) -> str:
    # The path is quoted so a space in it can't split the command.
    quoted = '"' + exec_path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Plan your workday and get reminded\n"
        f"Exec={quoted} --background\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-UsesNotifications=true\n"
    )


def is_enabled(path: Path) -> bool:
    return path.is_file()


def enable(path: Path, exec_path: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry_text(exec_path), encoding="utf-8")
    log.info("start at login on: %s", path)


def disable(path: Path) -> None:
    path.unlink(missing_ok=True)
    log.info("start at login off")


def apply(path: Path, exec_path: str, *, wanted: bool) -> None:
    """Make the file match the setting (also repairs a moved launcher)."""
    if wanted:
        if not is_enabled(path) or path.read_text(encoding="utf-8") != entry_text(exec_path):
            enable(path, exec_path)
    elif is_enabled(path):
        disable(path)
