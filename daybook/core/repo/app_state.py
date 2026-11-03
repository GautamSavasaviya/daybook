"""Small internal state that must survive restarts and be included in backups.

Examples: the reminder watermark, the last rollover date, whether onboarding is done.
Values are JSON.
"""

import json

from daybook.core.db import Connection, transaction

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def get(conn: Connection, key: str, default: JsonValue = None) -> JsonValue:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    value: JsonValue = json.loads(row["value"])
    return value


def put(conn: Connection, key: str, value: JsonValue) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def delete(conn: Connection, key: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
