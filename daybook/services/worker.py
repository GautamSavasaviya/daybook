"""Run slow database work off the main loop (D-041).

One worker thread with its own connection (WAL lets it read while the main thread writes).
Results and errors are delivered back on the main thread through `call_soon`.
"""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from daybook.core.db import Connection, connect
from daybook.services.events import CallSoon
from daybook.services.safe import report_error, safe_callback

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, db_file: Path, call_soon: CallSoon) -> None:
        self._db_file = db_file
        self._call_soon = call_soon
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daybook-worker")
        self._pending: set[Future[Any]] = set()
        self._lock = threading.Lock()  # guards _pending and _closed across threads
        self._closed = False

    def _connection(self) -> Connection:
        conn: Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._db_file)
            self._local.conn = conn
        return conn

    def submit[T](
        self,
        job: Callable[[Connection], T],
        on_done: Callable[[T], None],
        on_error: Callable[[BaseException], None] = report_error,
    ) -> Future[T]:
        """Run `job(connection)` on the worker; call `on_done(result)` on the main thread.

        Nothing is delivered once `shutdown()` has started: by then the database and the
        widgets that asked for the result may already be gone.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("worker is shut down")
            future = self._executor.submit(lambda: job(self._connection()))
            self._pending.add(future)
        done_cb = safe_callback(on_done)
        error_cb = safe_callback(on_error)

        def deliver(finished: Future[T]) -> None:
            with self._lock:
                self._pending.discard(finished)
            if finished.cancelled() or self._closed:
                return
            exc = finished.exception()
            if exc is not None:
                self._call_soon(lambda: self._unless_closed(lambda: error_cb(exc)))
            else:
                result = finished.result()
                self._call_soon(lambda: self._unless_closed(lambda: done_cb(result)))

        future.add_done_callback(deliver)
        return future

    def _unless_closed(self, callback: Callable[[], object]) -> None:
        # Runs on the main thread; a result queued just before shutdown is dropped here.
        if not self._closed:
            callback()

    def shutdown(self) -> None:
        """Finish the running job, drop queued ones and close the worker's connection."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            queued = list(self._pending)
        for future in queued:
            future.cancel()  # only succeeds for jobs that have not started yet

        def close() -> None:
            conn: Connection | None = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

        self._executor.submit(close).result()
        self._executor.shutdown(wait=True)
