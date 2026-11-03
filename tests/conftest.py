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
