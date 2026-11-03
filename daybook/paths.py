"""Where Daybook keeps its files (XDG base directories; D-020 for DAYBOOK_DATA_DIR)."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from daybook.const import APP_ID

DATA_DIR_VAR = "DAYBOOK_DATA_DIR"
# Dev overrides that must not be silently forwarded to an already-running instance (D-020).
DEV_OVERRIDE_VARS = (DATA_DIR_VAR, "DAYBOOK_TIME_SCALE", "DAYBOOK_FAKE_NOW")


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    log_dir: Path
    config_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "daybook.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "daybook.log"

    @property
    def autostart_file(self) -> Path:
        return self.config_dir / "autostart" / f"{APP_ID}.desktop"


def _xdg(environ: Mapping[str, str], var: str, home: Path, fallback: str) -> Path:
    value = environ.get(var, "")
    # The XDG spec says relative paths are invalid and must be ignored.
    if value and Path(value).is_absolute():
        return Path(value)
    return home / fallback


def resolve_paths(environ: Mapping[str, str], home: Path | None = None) -> Paths:
    home = home or Path(environ.get("HOME") or Path.home())
    config_dir = _xdg(environ, "XDG_CONFIG_HOME", home, ".config")
    override = environ.get(DATA_DIR_VAR, "").strip()
    if override:
        data_dir = Path(override).expanduser().resolve()
        return Paths(data_dir=data_dir, log_dir=data_dir / "logs", config_dir=config_dir)
    return Paths(
        data_dir=_xdg(environ, "XDG_DATA_HOME", home, ".local/share") / "daybook",
        log_dir=_xdg(environ, "XDG_STATE_HOME", home, ".local/state") / "daybook",
        config_dir=config_dir,
    )


def active_dev_overrides(environ: Mapping[str, str]) -> list[str]:
    return [name for name in DEV_OVERRIDE_VARS if environ.get(name, "").strip()]
