from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from daybook.core.clock import ManualClock
from daybook.core.db import Connection, connect
from daybook.core.migrations import migrate
from daybook.services import safe

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START, tz=BERLIN)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "daybook.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[Connection]:
    connection = connect(db_path)
    migrate(connection, START)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _no_global_error_reporter() -> Iterator[None]:
    safe.set_error_reporter(None)
    yield
    safe.set_error_reporter(None)


@pytest.fixture(autouse=True)
def _silent_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever plays a real sound: reminders fired by the scheduler tests would beep
    through the speakers. The one test that checks playback installs its own fake."""
    from daybook.services import notifier  # noqa: PLC0415 — imports GLib, only needed here

    monkeypatch.setattr(notifier, "_load_gsound", lambda: None)
