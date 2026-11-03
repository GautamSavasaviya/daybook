"""Persistence for `Settings` (the ``settings`` table, one JSON value per key)."""

from daybook.core import settings as settings_model
from daybook.core.db import Connection, transaction
from daybook.core.settings import Settings


def load(conn: Connection) -> Settings:
    rows = {
        str(row["key"]): str(row["value"])
        for row in conn.execute("SELECT key, value FROM settings")
    }
    return settings_model.from_rows(rows)


def save(conn: Connection, new: Settings) -> None:
    """Validate and store every setting atomically."""
    settings_model.validate(new)
    with transaction(conn):
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            settings_model.to_rows(new).items(),
        )
