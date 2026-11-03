import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from daybook import logsetup
from daybook.const import APP_ID, DEVEL_APP_ID
from daybook.paths import (
    active_dev_overrides,
    application_id,
    dev_override_problem,
    resolve_paths,
)

HOME = Path("/home/tester")


def test_default_xdg_paths():
    paths = resolve_paths({}, home=HOME)
    assert paths.data_dir == HOME / ".local/share/daybook"
    assert paths.db_file == HOME / ".local/share/daybook/daybook.db"
    assert paths.backups_dir == HOME / ".local/share/daybook/backups"
    assert paths.log_file == HOME / ".local/state/daybook/daybook.log"
    assert paths.autostart_file == HOME / ".config/autostart/io.github.daybook.Daybook.desktop"


def test_xdg_variables_are_respected_and_relative_ones_ignored():
    paths = resolve_paths(
        {"XDG_DATA_HOME": "/data", "XDG_STATE_HOME": "relative/state", "XDG_CONFIG_HOME": "/cfg"},
        home=HOME,
    )
    assert paths.data_dir == Path("/data/daybook")
    assert paths.log_dir == HOME / ".local/state/daybook"
    assert paths.autostart_file.parent == Path("/cfg/autostart")


def test_data_dir_override_holds_data_and_logs(tmp_path):
    paths = resolve_paths({"DAYBOOK_DATA_DIR": str(tmp_path)}, home=HOME)
    assert paths.db_file == tmp_path / "daybook.db"
    assert paths.log_file == tmp_path / "logs" / "daybook.log"
    assert paths.autostart_file == HOME / ".config/autostart/io.github.daybook.Daybook.desktop"


def test_active_dev_overrides():
    assert active_dev_overrides({}) == []
    assert active_dev_overrides({"DAYBOOK_DATA_DIR": " "}) == []
    assert active_dev_overrides({"DAYBOOK_TIME_SCALE": "60", "DAYBOOK_FAKE_NOW": "x"}) == [
        "DAYBOOK_TIME_SCALE",
        "DAYBOOK_FAKE_NOW",
    ]


def test_test_runs_use_their_own_application_id():
    assert application_id({}) == APP_ID
    assert DEVEL_APP_ID == APP_ID + ".Devel"
    for var in ("DAYBOOK_DATA_DIR", "DAYBOOK_TIME_SCALE", "DAYBOOK_FAKE_NOW"):
        assert application_id({var: "x"}) == DEVEL_APP_ID


@pytest.mark.parametrize(
    "environ",
    [
        {"DAYBOOK_TIME_SCALE": "60"},
        {"DAYBOOK_FAKE_NOW": "2026-11-03T09:00"},
        {"DAYBOOK_TIME_SCALE": "60", "DAYBOOK_DATA_DIR": "  "},
    ],
)
def test_fake_time_requires_a_throwaway_data_dir(environ):
    problem = dev_override_problem(environ)
    assert problem is not None
    assert "DAYBOOK_DATA_DIR" in problem


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"DAYBOOK_DATA_DIR": "/tmp/x"},
        {"DAYBOOK_DATA_DIR": "/tmp/x", "DAYBOOK_TIME_SCALE": "60", "DAYBOOK_FAKE_NOW": "x"},
    ],
)
def test_allowed_dev_variable_combinations(environ):
    assert dev_override_problem(environ) is None


@pytest.fixture
def restore_logging_and_hooks():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    hooks = (sys.excepthook, threading.excepthook)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(capture=False)
    sys.excepthook, threading.excepthook = hooks


@pytest.mark.usefixtures("restore_logging_and_hooks")
def test_setup_logging_rotates_five_files_of_one_megabyte(tmp_path):
    log_file = tmp_path / "state" / "daybook.log"
    logsetup.setup_logging(log_file, debug=False)
    root = logging.getLogger()
    [handler] = root.handlers
    assert isinstance(handler, RotatingFileHandler)
    assert (handler.maxBytes, handler.backupCount) == (1_000_000, 4)
    assert root.level == logging.INFO
    logging.getLogger("daybook.test").info("hello")
    handler.flush()
    assert "hello" in log_file.read_text()


@pytest.mark.usefixtures("restore_logging_and_hooks")
def test_debug_logging_also_writes_to_the_terminal(tmp_path):
    logsetup.setup_logging(tmp_path / "daybook.log", debug=True)
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(type(h) is logging.StreamHandler for h in root.handlers)


@pytest.mark.usefixtures("restore_logging_and_hooks")
def test_excepthook_logs_and_reports(caplog):
    reported = []
    logsetup.install_excepthooks(reported.append)
    error = ValueError("boom")
    with caplog.at_level(logging.CRITICAL):
        sys.excepthook(ValueError, error, None)
    assert reported == [error]
    assert "uncaught exception" in caplog.text


@pytest.mark.usefixtures("restore_logging_and_hooks")
def test_excepthook_survives_a_failing_reporter(caplog):
    def broken(_exc):
        raise RuntimeError("reporter broke")

    logsetup.install_excepthooks(broken)
    with caplog.at_level(logging.ERROR):
        sys.excepthook(KeyError, KeyError("k"), None)
    assert "error while reporting" in caplog.text


@pytest.mark.usefixtures("restore_logging_and_hooks")
def test_keyboard_interrupt_goes_to_the_default_hook(monkeypatch):
    reported, default = [], []
    monkeypatch.setattr(sys, "__excepthook__", lambda *args: default.append(args[0]))
    logsetup.install_excepthooks(reported.append)
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert reported == []
    assert default == [KeyboardInterrupt]


@pytest.mark.usefixtures("restore_logging_and_hooks")
def test_thread_exceptions_are_reported(caplog):
    reported = []
    logsetup.install_excepthooks(reported.append)

    def crash():
        raise OSError("in thread")

    with caplog.at_level(logging.CRITICAL):
        thread = threading.Thread(target=crash, name="crasher")
        thread.start()
        thread.join()
    assert [str(e) for e in reported] == ["in thread"]
    assert "crasher" in caplog.text
