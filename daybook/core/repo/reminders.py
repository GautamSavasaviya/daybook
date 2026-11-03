"""The reminder log (at-most-once delivery, D-024), snoozes and the last-check watermark."""

from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum

from daybook.core import timeutil
from daybook.core.db import Connection, transaction
from daybook.core.reminders import Action, Reminder
from daybook.core.repo import app_state

WATERMARK = "last_check_utc"


class Status(StrEnum):
    FIRED = "fired"
    MISSED = "missed"
    SUPPRESSED = "suppressed"


def logged(conn: Connection, keys: Iterable[str]) -> set[str]:
    wanted = list(dict.fromkeys(keys))
    found: set[str] = set()
    # SQLite limits host parameters; 500 at a time stays well inside it.
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ", ".join("?" * len(chunk))
        rows = conn.execute(f"SELECT key FROM reminder_log WHERE key IN ({marks})", chunk)
        found.update(row["key"] for row in rows)
    return found


def log(conn: Connection, reminder: Reminder, status: Status, now: datetime) -> bool:
    """Record a reminder occurrence. True only for the first caller for that key: only it
    may send the notification (D-024). A snooze that fires leaves the pending list."""
    with transaction(conn):
        row = conn.execute(
            "INSERT INTO reminder_log (key, kind, due_utc, logged_utc, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO NOTHING RETURNING id",
            (
                reminder.key,
                reminder.kind.value,
                timeutil.to_db(reminder.due),
                timeutil.to_db(now),
                status.value,
                reminder.to_json(),
            ),
        ).fetchone()
        conn.execute("DELETE FROM pending_reminder WHERE key = ?", (reminder.key,))
        return row is not None


def log_missed(conn: Connection, reminders: Sequence[Reminder], now: datetime) -> None:
    with transaction(conn):
        for reminder in reminders:
            log(conn, reminder, Status.MISSED, now)


def get(conn: Connection, key: str) -> Reminder | None:
    row = conn.execute("SELECT payload FROM reminder_log WHERE key = ?", (key,)).fetchone()
    if row is None:
        row = conn.execute("SELECT payload FROM pending_reminder WHERE key = ?", (key,)).fetchone()
    return Reminder.from_json(row["payload"]) if row is not None else None


def record_action(conn: Connection, key: str, action: Action, now: datetime) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE reminder_log SET action = ?, action_utc = ? WHERE key = ?",
            (action.value, timeutil.to_db(now), key),
        )


def snoozes(conn: Connection) -> list[Reminder]:
    rows = conn.execute("SELECT payload FROM pending_reminder ORDER BY due_utc")
    return [Reminder.from_json(row["payload"]) for row in rows]


def snooze_count(conn: Connection, base_key: str) -> int:
    pattern = base_key.replace("%", r"\%").replace("_", r"\_") + ":snooze%"
    row = conn.execute(
        "SELECT (SELECT count(*) FROM reminder_log WHERE key LIKE ? ESCAPE '\\') + "
        "(SELECT count(*) FROM pending_reminder WHERE key LIKE ? ESCAPE '\\')",
        (pattern, pattern),
    ).fetchone()
    return int(row[0])


def add_snooze(conn: Connection, reminder: Reminder) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO pending_reminder (key, kind, due_utc, payload) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key) DO NOTHING",
            (reminder.key, reminder.kind.value, timeutil.to_db(reminder.due), reminder.to_json()),
        )


def watermark(conn: Connection) -> datetime | None:
    value = app_state.get(conn, WATERMARK)
    return timeutil.from_db(value) if isinstance(value, str) else None


def set_watermark(conn: Connection, instant: datetime) -> None:
    app_state.put(conn, WATERMARK, timeutil.to_db(instant))
