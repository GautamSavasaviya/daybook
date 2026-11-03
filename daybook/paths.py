"""Where Daybook keeps its files (XDG base directories) and which app ID a run uses (D-065)."""

from collections.abc import Mapping
from dataclasses import dataclass
from gettext import gettext as _
from pathlib import Path

from daybook.const import APP_ID, DEVEL_APP_ID

DATA_DIR_VAR = "DAYBOOK_DATA_DIR"
TIME_VARS = ("DAYBOOK_TIME_SCALE", "DAYBOOK_FAKE_NOW")
# Any of these makes this a test run with its own application ID (D-065).
DEV_OVERRIDE_VARS = (DATA_DIR_VAR, *TIME_VARS)


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


def application_id(environ: Mapping[str, str]) -> str:
    """The real APP_ID, or the separate Devel ID for test runs (D-065)."""
    return DEVEL_APP_ID if active_dev_overrides(environ) else APP_ID


def dev_override_problem(environ: Mapping[str, str]) -> str | None:
    """Why these dev variables must not be used, or None if they are fine.

    Fake or fast time on the real database would write misleading timestamps into real
    data, so the time variables require a throwaway DAYBOOK_DATA_DIR (D-065).
    """
    time_vars = [name for name in TIME_VARS if environ.get(name, "").strip()]
    if time_vars and not environ.get(DATA_DIR_VAR, "").strip():
        return _(
            "Set {data_dir} as well when using {variables}, so fake time never touches your "
            "real data. Example: {data_dir}=$(mktemp -d) {first}=60 daybook"
        ).format(variables=" and ".join(time_vars), data_dir=DATA_DIR_VAR, first=time_vars[0])
    return None
