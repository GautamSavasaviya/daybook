import sqlite3

import pytest

from daybook.core.db import connect, execute_script, transaction


@pytest.fixture
def plain(db_path):
    conn = connect(db_path)
    conn.execute("CREATE TABLE t (v INTEGER NOT NULL)")
    yield conn
    conn.close()


def values(conn):
    return [row["v"] for row in conn.execute("SELECT v FROM t ORDER BY v")]


def test_connection_pragmas(db_path):
    conn = connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


def test_transaction_commits(plain):
    with transaction(plain):
        plain.execute("INSERT INTO t VALUES (1)")
    assert values(plain) == [1]
    assert not plain.in_transaction


def test_transaction_rolls_back_on_error(plain):
    with pytest.raises(RuntimeError), transaction(plain):
        plain.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")
    assert values(plain) == []
    assert plain.tx_depth == 0


def test_nested_transaction_rolls_back_only_the_inner_part(plain):
    with transaction(plain):
        plain.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(ValueError, match="inner"), transaction(plain):
            plain.execute("INSERT INTO t VALUES (2)")
            raise ValueError("inner")
        with transaction(plain):
            plain.execute("INSERT INTO t VALUES (3)")
    assert values(plain) == [1, 3]


def test_outer_failure_rolls_back_committed_inner_savepoints(plain):
    with pytest.raises(KeyError), transaction(plain):
        with transaction(plain):
            plain.execute("INSERT INTO t VALUES (1)")
        raise KeyError("outer")
    assert values(plain) == []


def test_read_only_connection_cannot_write(db_path, plain):
    with transaction(plain):
        plain.execute("INSERT INTO t VALUES (7)")
    ro = connect(db_path, read_only=True)
    assert [row["v"] for row in ro.execute("SELECT v FROM t")] == [7]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO t VALUES (8)")
    ro.close()


def test_execute_script_keeps_trigger_bodies_and_skips_comments(plain):
    script = """
    -- a comment; with a semicolon
    CREATE TABLE log (msg TEXT);
    CREATE TRIGGER t_ins AFTER INSERT ON t
    BEGIN INSERT INTO log VALUES ('inserted'); INSERT INTO log VALUES ('twice'); END;
    -- trailing comment
    """
    with transaction(plain):
        execute_script(plain, script)
        plain.execute("INSERT INTO t VALUES (1)")
    assert [r["msg"] for r in plain.execute("SELECT msg FROM log")] == ["inserted", "twice"]


def test_execute_script_rejects_an_unfinished_statement(plain):
    with pytest.raises(ValueError, match="incomplete"):
        execute_script(plain, "CREATE TABLE x (a INTEGER)")
