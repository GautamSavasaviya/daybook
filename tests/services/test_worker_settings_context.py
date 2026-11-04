import dataclasses
import sqlite3
import threading
from datetime import time, timedelta

import pytest

from daybook.core.db import connect
from daybook.core.errors import SchemaTooNewError, ValidationError
from daybook.core.migrations import LATEST_VERSION, schema_version
from daybook.core.models import TaskDraft
from daybook.paths import Paths
from daybook.services import safe
from daybook.services.context import AppContext
from daybook.services.events import Changed, Entity, EventBus
from daybook.services.settings import SettingsService
from daybook.services.worker import Worker


class Queue:
    def __init__(self) -> None:
        self.callbacks = []
        self.lock = threading.Lock()

    def __call__(self, callback) -> None:
        with self.lock:
            self.callbacks.append(callback)

    def run(self) -> None:
        with self.lock:
            pending, self.callbacks = self.callbacks, []
        for callback in pending:
            callback()


# --- Worker ---------------------------------------------------------------------------


def test_worker_runs_jobs_on_its_own_thread_and_delivers_on_main(conn, db_path):
    queue = Queue()
    worker = Worker(db_path, queue)
    results = []

    def job(worker_conn):
        name = threading.current_thread().name
        count = worker_conn.execute("SELECT count(*) FROM project").fetchone()[0]
        return name, count, worker_conn is conn

    worker.submit(job, results.append).result(timeout=5)
    assert results == []  # not delivered until the main loop runs
    queue.run()
    [(thread_name, count, same_conn)] = results
    assert thread_name.startswith("daybook-worker")
    assert count == 1
    assert same_conn is False
    worker.shutdown()


def test_worker_errors_go_to_on_error(conn, db_path):
    queue = Queue()
    worker = Worker(db_path, queue)
    errors = []

    def job(worker_conn):
        worker_conn.execute("SELECT * FROM no_such_table")

    future = worker.submit(job, lambda _r: None, errors.append)
    with pytest.raises(sqlite3.OperationalError):
        future.result(timeout=5)
    queue.run()
    assert [type(e) for e in errors] == [sqlite3.OperationalError]
    worker.shutdown()


def test_worker_default_error_handler_reports(conn, db_path):
    queue = Queue()
    worker = Worker(db_path, queue)
    reported = []
    safe.set_error_reporter(reported.append)
    future = worker.submit(lambda _c: 1 / 0, lambda _r: None)
    with pytest.raises(ZeroDivisionError):
        future.result(timeout=5)
    queue.run()
    assert [type(e) for e in reported] == [ZeroDivisionError]
    worker.shutdown()


def test_worker_shutdown_drops_queued_jobs_and_refuses_new_ones(conn, db_path):
    queue = Queue()
    worker = Worker(db_path, queue)
    release = threading.Event()
    started = threading.Event()

    def slow(_c):
        started.set()
        release.wait(5)
        return "slow"

    first = worker.submit(slow, lambda _r: None)
    queued = worker.submit(lambda _c: "never", lambda _r: None)
    started.wait(5)
    threading.Timer(0.05, release.set).start()
    worker.shutdown()
    assert first.result() == "slow"
    assert queued.cancelled()
    worker.shutdown()  # idempotent
    with pytest.raises(RuntimeError, match="shut down"):
        worker.submit(lambda _c: None, lambda _r: None)


def test_results_finishing_around_shutdown_are_not_delivered(conn, db_path):
    queue = Queue()
    worker = Worker(db_path, queue)
    delivered = []
    worker.submit(lambda _c: "late result", delivered.append).result(timeout=5)
    # The result is queued for the main loop, but the app shuts down before the loop runs.
    worker.shutdown()
    queue.run()
    assert delivered == []


# --- SettingsService -------------------------------------------------------------------


def test_settings_update_persists_and_publishes(conn):
    bus = EventBus(Queue())
    events = []
    bus.subscribe(Changed, events.append)
    service = SettingsService(conn, bus)
    updated = service.save(
        dataclasses.replace(service.current, work_end=time(17, 30), sound_enabled=False)
    )
    assert service.current == updated
    assert SettingsService(conn, bus).current == updated
    assert events == [Changed(Entity.SETTINGS)]


def test_invalid_settings_update_changes_nothing(conn):
    bus = EventBus(Queue())
    events = []
    bus.subscribe(Changed, events.append)
    service = SettingsService(conn, bus)
    before = service.current
    with pytest.raises(ValidationError):
        service.save(dataclasses.replace(before, work_end=time(6)))
    assert service.current == before
    assert events == []


# --- AppContext ------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path):
    return Paths(data_dir=tmp_path / "data", log_dir=tmp_path / "logs", config_dir=tmp_path / "cfg")


def test_context_creates_and_migrates_the_database(paths, clock):
    context = AppContext.open(paths, clock, Queue())
    assert paths.db_file.exists()
    assert schema_version(context.conn) == LATEST_VERSION
    assert context.settings.current.work_start == time(9)
    context.close()
    with pytest.raises(sqlite3.ProgrammingError):
        context.conn.execute("SELECT 1")
    reopened = AppContext.open(paths, clock, Queue())
    reopened.close()


def test_closing_the_context_pauses_a_running_timer(paths, clock):
    context = AppContext.open(paths, clock, Queue())
    task_id = context.tasks.create(TaskDraft(title="Timed"))
    context.timer.start(task_id)
    clock.advance(timedelta(minutes=5))
    context.close()
    reopened = AppContext.open(paths, clock, Queue())
    state = reopened.timer.state()
    assert state is not None
    assert not state.running
    assert reopened.timer.pending_recovery() is None
    assert reopened.timer.tracked_today_s(task_id) == 300
    reopened.close()


def test_context_refuses_a_newer_database_without_touching_it(paths, clock):
    paths.data_dir.mkdir(parents=True)
    db = sqlite3.connect(paths.db_file)
    db.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    db.close()
    before = paths.db_file.read_bytes()
    with pytest.raises(SchemaTooNewError):
        AppContext.open(paths, clock, Queue())
    assert paths.db_file.read_bytes() == before
    assert not paths.db_file.with_name("daybook.db-wal").exists()


def test_context_reports_a_corrupt_database_without_touching_it(paths, clock):
    paths.data_dir.mkdir(parents=True)
    paths.db_file.write_bytes(b"this is not a database" * 100)
    before = paths.db_file.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        AppContext.open(paths, clock, Queue())
    assert paths.db_file.read_bytes() == before


def test_context_closes_the_connection_when_migration_fails(paths, clock, monkeypatch):
    opened = []
    real_connect = connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    def failing_migrate(_conn, _now):
        raise RuntimeError("disk full")

    monkeypatch.setattr("daybook.services.context.connect", tracking_connect)
    monkeypatch.setattr("daybook.services.context.migrate", failing_migrate)
    with pytest.raises(RuntimeError, match="disk full"):
        AppContext.open(paths, clock, Queue())
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("SELECT 1")
