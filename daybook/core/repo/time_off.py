"""Time off (D-048): leave and holidays, one row per local date."""

from datetime import date

from daybook.core import timeutil, validation
from daybook.core.db import Connection, transaction
from daybook.core.models import TimeOff, TimeOffPart


def all_days(conn: Connection) -> dict[date, TimeOffPart]:
    return {
        timeutil.date_from_db(row["day"]): TimeOffPart(row["part"])
        for row in conn.execute("SELECT day, part FROM time_off")
    }


def get(conn: Connection, day: date) -> TimeOff | None:
    row = conn.execute(
        "SELECT day, part, note FROM time_off WHERE day = ?", (timeutil.date_to_db(day),)
    ).fetchone()
    if row is None:
        return None
    return TimeOff(timeutil.date_from_db(row["day"]), TimeOffPart(row["part"]), row["note"])


def from_day(conn: Connection, first: date) -> list[TimeOff]:
    rows = conn.execute(
        "SELECT day, part, note FROM time_off WHERE day >= ? ORDER BY day",
        (timeutil.date_to_db(first),),
    )
    return [
        TimeOff(timeutil.date_from_db(row["day"]), TimeOffPart(row["part"]), row["note"])
        for row in rows
    ]


def put(conn: Connection, entry: TimeOff) -> None:
    note = validation.optional_text(entry.note, "note") or ""
    with transaction(conn):
        conn.execute(
            "INSERT INTO time_off (day, part, note) VALUES (?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET part = excluded.part, note = excluded.note",
            (timeutil.date_to_db(entry.day), TimeOffPart(entry.part).value, note),
        )


def remove(conn: Connection, day: date) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM time_off WHERE day = ?", (timeutil.date_to_db(day),))
