"""SQLite connections and transactions.

Every connection runs in WAL mode with foreign keys on. Writes go through `transaction()`,
which opens ``BEGIN IMMEDIATE`` (or a savepoint when nested) and rolls back on any error,
so a failure never leaves a partial write behind (SPEC, Architecture).
"""

import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 5000

log = logging.getLogger(__name__)


class Connection(sqlite3.Connection):
    """sqlite3 connection that tracks transaction nesting for `transaction()`."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.tx_depth = 0


def connect(path: Path | str, *, read_only: bool = False) -> Connection:
    """Open the database. ``read_only`` connections never create or modify the file."""
    if read_only:
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, autocommit=True, factory=Connection)
    else:
        conn = sqlite3.connect(path, autocommit=True, factory=Connection)
    conn.row_factory = sqlite3.Row
    # Unicode-aware case folding for search; SQLite's own LIKE/lower() only fold ASCII.
    conn.create_function("fold", 1, _fold, deterministic=True)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _fold(text: str | None) -> str | None:
    return None if text is None else text.casefold()


@contextmanager
def transaction(conn: Connection) -> Generator[Connection]:
    """Run a block atomically. Nested calls become savepoints of the outer transaction.

    If the block or the final COMMIT fails, everything is rolled back and the connection is
    left ready for the next transaction. The original error is always the one raised.
    """
    depth = conn.tx_depth
    if depth == 0:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT sp{depth}")
    conn.tx_depth = depth + 1
    try:
        yield conn
        if depth == 0:
            conn.execute("COMMIT")
        else:
            conn.execute(f"RELEASE sp{depth}")
    except BaseException:
        _rollback(conn, depth)
        raise
    finally:
        conn.tx_depth = depth


def _rollback(conn: Connection, depth: int) -> None:
    # SQLite may already have rolled the whole transaction back itself (SQLITE_FULL,
    # RAISE(ROLLBACK), ...); then there is nothing left to undo.
    if not conn.in_transaction:
        return
    try:
        if depth == 0:
            conn.execute("ROLLBACK")
        else:
            conn.execute(f"ROLLBACK TO sp{depth}")
            conn.execute(f"RELEASE sp{depth}")
    except sqlite3.Error:
        log.exception("rollback failed; the original error follows")


def execute_script(conn: Connection, sql: str) -> None:
    """Execute several statements without the implicit COMMIT of `executescript()`.

    Statements are split with `sqlite3.complete_statement`, so trigger bodies containing
    semicolons stay intact. Safe to call inside `transaction()`.
    """
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement.rstrip(";").strip():
                conn.execute(statement)
            buffer = ""
    if buffer.strip() and not _only_comments(buffer):
        raise ValueError(f"incomplete SQL statement: {buffer.strip()[:80]!r}")


def _only_comments(text: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in text.splitlines())
