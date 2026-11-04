"""JSON export and import (D-026), and the time-entry CSV. Pure of GUI code.

Export writes every table that holds your data as rows of column → value. Import checks the
file *completely* before anything is written: it builds the data in a scratch in-memory
database made with the real schema, so every constraint, reference and the overlap triggers
reject bad data exactly as the app would. Only if all of it fits is the live data replaced.
"""

import csv
import io
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from daybook.core import timeutil
from daybook.core.db import Connection
from daybook.core.errors import DaybookError
from daybook.core.migrations import LATEST_VERSION, MIGRATIONS, migrate, schema_version

FORMAT = "daybook-export"
FORMAT_VERSION = 1
# In dependency order: a row's references come before it. Reminder history and pending
# snoozes are not data worth keeping; app state is rebuilt (onboarding is marked done).
TABLES = (
    "project",
    "task",
    "checklist_item",
    "tag",
    "task_tag",
    "meeting",
    "meeting_exception",
    "day_plan_item",
    "time_off",
    "time_entry",
    "timer_state",
    "settings",
)
MAX_PROBLEMS = 8
_SCALAR = (str, int, float, type(None))


@dataclass(frozen=True)
class Problem:
    path: str  # e.g. "tables.task[3].project_id"
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ImportRefusedError(DaybookError):
    """The file can't be imported; nothing was changed. `problems` say where and why."""

    def __init__(self, problems: list[Problem]) -> None:
        shown = "; ".join(str(p) for p in problems[:MAX_PROBLEMS])
        more = len(problems) - MAX_PROBLEMS
        super().__init__(shown + (f"; and {more} more" if more > 0 else ""))
        self.problems = problems


def export_data(conn: Connection, now: datetime) -> str:
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        cursor = conn.execute(f"SELECT * FROM {table}")  # table names are constants above
        tables[table] = [dict(row) for row in cursor]
    document = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "schema_version": schema_version(conn),
        "exported_at": timeutil.to_db(now),
        "tables": tables,
    }
    return json.dumps(document, ensure_ascii=False, indent=1) + "\n"


@dataclass
class Validated:
    """A file that passed every check, as a ready in-memory database."""

    conn: Connection
    counts: dict[str, int]

    def close(self) -> None:
        self.conn.close()


def _shape(document: object) -> tuple[int, Mapping[str, list[Mapping[str, Any]]]]:
    if not isinstance(document, dict):
        raise ImportRefusedError([Problem("$", "This isn't a Daybook export file.")])
    if document.get("format") != FORMAT:
        raise ImportRefusedError([Problem("format", "This isn't a Daybook export file.")])
    version = document.get("format_version")
    if not isinstance(version, int) or version > FORMAT_VERSION or version < 1:
        raise ImportRefusedError(
            [Problem("format_version", f"Unsupported export version {version!r}.")]
        )
    schema = document.get("schema_version")
    if not isinstance(schema, int) or not 1 <= schema <= LATEST_VERSION:
        newer = isinstance(schema, int) and schema > LATEST_VERSION
        message = (
            "It was made by a newer Daybook; update Daybook first." if newer else "Bad schema."
        )
        raise ImportRefusedError([Problem("schema_version", message)])
    tables = document.get("tables")
    if not isinstance(tables, dict):
        raise ImportRefusedError([Problem("tables", "The data is missing.")])
    problems = [
        Problem(f"tables.{name}", "Unknown table.") for name in tables if name not in TABLES
    ]
    problems += [
        Problem(f"tables.{name}", "Missing table.") for name in TABLES if name not in tables
    ]
    for name, rows in tables.items():
        if name in TABLES and not isinstance(rows, list):
            problems.append(Problem(f"tables.{name}", "Expected a list of rows."))
    if problems:
        raise ImportRefusedError(problems)
    return schema, tables


def _insert(
    mem: sqlite3.Connection, table: str, rows: list[Mapping[str, Any]], problems: list[Problem]
) -> None:
    columns = [r[1] for r in mem.execute(f"PRAGMA table_info({table})")]
    for index, row in enumerate(rows):
        where = f"tables.{table}[{index}]"
        if not isinstance(row, dict):
            problems.append(Problem(where, "Expected a row."))
            continue
        extra = sorted(set(row) - set(columns))
        if extra:
            problems.append(Problem(where, f"Unknown column {extra[0]!r}."))
            continue
        bad = next((k for k, v in row.items() if not isinstance(v, _SCALAR)), None)
        if bad is not None:
            problems.append(Problem(f"{where}.{bad}", "Expected text, a number or null."))
            continue
        names = list(row)
        marks = ", ".join("?" * len(names))
        # The new database already holds the seeded General area, which the schema forbids
        # deleting; the file's own General (id 1) replaces it.
        verb = "INSERT OR REPLACE" if table == "project" and row.get("id") == 1 else "INSERT"
        sql = f"{verb} INTO {table} ({', '.join(names)}) VALUES ({marks})"
        try:
            mem.execute(sql, [row[n] for n in names])
        except sqlite3.IntegrityError as exc:
            problems.append(Problem(where, _explain(str(exc))))
        except sqlite3.Error as exc:
            problems.append(Problem(where, str(exc)))
        if len(problems) >= MAX_PROBLEMS:
            return


def _explain(message: str) -> str:
    if "time_entry_overlap" in message:
        return "This time overlaps another time entry."
    if "FOREIGN KEY" in message:
        return "It refers to something that isn't in the file."
    if "UNIQUE" in message:
        return "It appears twice."
    if "CHECK" in message:
        return f"A value isn't allowed ({message.removeprefix('CHECK constraint failed: ')})."
    return message


def validate(text: str, now: datetime) -> Validated:
    """Check the whole file. Raises ImportRefusedError, with where and why, and writes nothing."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ImportRefusedError([Problem("$", f"This isn't valid JSON ({exc}).")]) from exc
    schema, tables = _shape(document)
    mem = sqlite3.connect(":memory:", autocommit=True, factory=Connection)
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA foreign_keys = ON")
    try:
        # The schema the file was made with; it is migrated up to the latest afterwards.
        migrate(mem, now, MIGRATIONS[:schema])
        problems: list[Problem] = []
        for table in TABLES:
            _insert(mem, table, list(tables[table]), problems)
            if len(problems) >= MAX_PROBLEMS:
                break
        if not problems and not any(r.get("id") == 1 for r in tables["project"]):
            problems.append(Problem("tables.project", "The General area (id 1) is missing."))
        if not problems:
            broken = mem.execute("PRAGMA foreign_key_check").fetchall()
            problems += [Problem(f"tables.{r[0]}", "A reference is broken.") for r in broken[:3]]
        if problems:
            raise ImportRefusedError(problems)
        mem.execute("INSERT INTO app_state (key, value) VALUES ('onboarding_done', 'true')")
        migrate(mem, now)
    except BaseException:
        mem.close()
        raise
    counts = {t: len(tables[t]) for t in TABLES}
    return Validated(mem, counts)


def time_entries_csv(rows: Sequence[Mapping[str, Any]], tz: tzinfo) -> str:
    """Every time entry as CSV: local date and times, minutes, ticket, task, area, note."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["date", "start", "end", "minutes", "ticket", "task", "project", "source", "note"]
    )
    for row in rows:
        start = timeutil.from_db(row["start_utc"]).astimezone(tz)
        end = timeutil.from_db(row["end_utc"]).astimezone(tz)
        minutes = round((end - start).total_seconds() / 60)
        writer.writerow(
            [
                start.date().isoformat(),
                start.strftime("%H:%M:%S"),
                end.strftime("%H:%M:%S"),
                minutes,
                row["ticket_key"] or "",
                row["title"],
                row["project_name"],
                row["source"],
                row["note"],
            ]
        )
    return out.getvalue()
