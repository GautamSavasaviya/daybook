"""Persistence for meetings, their single-occurrence changes and ad-hoc meetings."""

import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, tzinfo
from gettext import gettext as _

from daybook.core import meetings as rules
from daybook.core import recurrence as recurrence_rules
from daybook.core import timeutil
from daybook.core.db import Connection, transaction
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import (
    Meeting,
    MeetingDraft,
    MeetingException,
    OccurrenceEdit,
    occurrence_key,
)
from daybook.core.repo import timer as timer_repo
from daybook.core.timer import Trim

log = logging.getLogger(__name__)

_COLUMNS = (
    "id, title, start_utc, duration_min, tzid, link, recurrence, reminder_offsets, "
    "project_id, is_adhoc, open_ended, created_at, updated_at, deleted_at"
)
_EXCEPTION_COLUMNS = (
    "id, meeting_id, original_start_utc, cancelled, title, start_utc, duration_min, link, "
    "reminder_offsets"
)


def _offsets(text: str | None) -> tuple[int, ...] | None:
    if text is None:
        return None
    values = json.loads(text)
    return tuple(int(v) for v in values) if isinstance(values, list) else ()


def _from_row(row: sqlite3.Row) -> Meeting:
    try:
        rule = recurrence_rules.from_json(row["recurrence"])
    except (ValueError, KeyError, ValidationError) as exc:
        log.warning(
            "meeting %d has an unreadable recurrence; treating it as one-off: %s", row["id"], exc
        )
        rule = None
    deleted = row["deleted_at"]
    return Meeting(
        id=row["id"],
        title=row["title"],
        start=timeutil.from_db(row["start_utc"]),
        duration_min=row["duration_min"],
        tzid=row["tzid"],
        link=row["link"],
        recurrence=rule,
        reminder_offsets=_offsets(row["reminder_offsets"]) or (),
        project_id=row["project_id"],
        is_adhoc=bool(row["is_adhoc"]),
        open_ended=bool(row["open_ended"]),
        created_at=timeutil.from_db(row["created_at"]),
        updated_at=timeutil.from_db(row["updated_at"]),
        deleted_at=timeutil.from_db(deleted) if deleted else None,
    )


def _exception_from_row(row: sqlite3.Row) -> MeetingException:
    start = row["start_utc"]
    return MeetingException(
        id=row["id"],
        meeting_id=row["meeting_id"],
        original_start=timeutil.from_db(row["original_start_utc"]),
        cancelled=bool(row["cancelled"]),
        title=row["title"],
        start=timeutil.from_db(start) if start else None,
        duration_min=row["duration_min"],
        link=row["link"],
        reminder_offsets=_offsets(row["reminder_offsets"]),
    )


def get(conn: Connection, meeting_id: int, *, include_deleted: bool = False) -> Meeting:
    row = conn.execute(f"SELECT {_COLUMNS} FROM meeting WHERE id = ?", (meeting_id,)).fetchone()
    if row is None or (row["deleted_at"] is not None and not include_deleted):
        raise NotFoundError("meeting", meeting_id)
    return _from_row(row)


def active(conn: Connection) -> list[Meeting]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM meeting WHERE deleted_at IS NULL ORDER BY start_utc, id"
    )
    return [_from_row(row) for row in rows]


def exceptions(
    conn: Connection, meeting_id: int | None = None
) -> dict[int, list[MeetingException]]:
    """Single-occurrence changes, grouped by meeting."""
    sql = f"SELECT {_EXCEPTION_COLUMNS} FROM meeting_exception"
    params: tuple[int, ...] = ()
    if meeting_id is not None:
        sql += " WHERE meeting_id = ?"
        params = (meeting_id,)
    grouped: dict[int, list[MeetingException]] = {}
    for row in conn.execute(sql + " ORDER BY original_start_utc", params):
        change = _exception_from_row(row)
        grouped.setdefault(change.meeting_id, []).append(change)
    return grouped


def _project_or_none(conn: Connection, project_id: int | None) -> int | None:
    if project_id is None:
        return None
    if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
        raise ValidationError("project", _("Choose an existing project or area."))
    return project_id


def create(
    conn: Connection,
    draft: MeetingDraft,
    tzid: str,
    now: datetime,
    *,
    is_adhoc: bool = False,
    open_ended: bool = False,
) -> int:
    clean = rules.validate_draft(draft)
    stamp = timeutil.to_db(now)
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO meeting (title, start_utc, duration_min, tzid, link, recurrence, "
            "reminder_offsets, project_id, is_adhoc, open_ended, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clean.title,
                timeutil.to_db(clean.start),
                clean.duration_min,
                tzid,
                clean.link,
                recurrence_rules.to_json(clean.recurrence) if clean.recurrence else None,
                json.dumps(list(clean.reminder_offsets)),
                _project_or_none(conn, clean.project_id),
                int(is_adhoc),
                int(open_ended),
                stamp,
                stamp,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def update_series(
    conn: Connection,
    meeting_id: int,
    draft: MeetingDraft,
    now: datetime,
    discard: Sequence[int] = (),
) -> None:
    """Change the whole series; `discard` lists exception ids that no longer match an
    occurrence (confirmed by the user first, D-013)."""
    clean = rules.validate_draft(draft)
    with transaction(conn):
        get(conn, meeting_id)
        conn.execute(
            "UPDATE meeting SET title = ?, start_utc = ?, duration_min = ?, link = ?, "
            "recurrence = ?, reminder_offsets = ?, project_id = ?, updated_at = ? WHERE id = ?",
            (
                clean.title,
                timeutil.to_db(clean.start),
                clean.duration_min,
                clean.link,
                recurrence_rules.to_json(clean.recurrence) if clean.recurrence else None,
                json.dumps(list(clean.reminder_offsets)),
                _project_or_none(conn, clean.project_id),
                timeutil.to_db(now),
                meeting_id,
            ),
        )
        conn.executemany(
            "DELETE FROM meeting_exception WHERE id = ? AND meeting_id = ?",
            [(exception_id, meeting_id) for exception_id in discard],
        )


def set_occurrence(
    conn: Connection,
    meeting: Meeting,
    original_start: datetime,
    *,
    cancelled: bool = False,
    edit: OccurrenceEdit | None = None,
    fallback: tzinfo = UTC,
) -> None:
    """Upsert the change for one occurrence. Edit fields equal to the series are stored as
    NULL, so later series edits still reach them (D-013)."""
    if not rules.is_occurrence(meeting, original_start, fallback):
        raise ValidationError("occurrence", _("That meeting doesn't happen at this time."))
    values: tuple[object, ...] = (None, None, None, None, None)
    if edit is not None:
        clean = rules.validate_edit(edit)
        values = (
            clean.title if clean.title != meeting.title else None,
            timeutil.to_db(clean.start) if clean.start != original_start else None,
            clean.duration_min if clean.duration_min != meeting.duration_min else None,
            # "" means "no link for this day" (NULL means "inherit the series' link").
            (clean.link or "") if clean.link != meeting.link else None,
            json.dumps(list(clean.reminder_offsets))
            if clean.reminder_offsets != meeting.reminder_offsets
            else None,
        )
    with transaction(conn):
        conn.execute(
            "INSERT INTO meeting_exception (meeting_id, original_start_utc, cancelled, title, "
            "start_utc, duration_min, link, reminder_offsets) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(meeting_id, original_start_utc) DO UPDATE SET "
            "cancelled = excluded.cancelled, title = excluded.title, "
            "start_utc = excluded.start_utc, duration_min = excluded.duration_min, "
            "link = excluded.link, reminder_offsets = excluded.reminder_offsets",
            (meeting.id, timeutil.to_db(original_start), int(cancelled), *values),
        )


def exception_for(
    conn: Connection, meeting_id: int, original_start: datetime
) -> MeetingException | None:
    row = conn.execute(
        f"SELECT {_EXCEPTION_COLUMNS} FROM meeting_exception "
        "WHERE meeting_id = ? AND original_start_utc = ?",
        (meeting_id, timeutil.to_db(original_start)),
    ).fetchone()
    return _exception_from_row(row) if row else None


def restore_exception(
    conn: Connection, meeting_id: int, original_start: datetime, previous: MeetingException | None
) -> None:
    """Put one occurrence's change back as it was (Undo)."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM meeting_exception WHERE meeting_id = ? AND original_start_utc = ?",
            (meeting_id, timeutil.to_db(original_start)),
        )
        if previous is None:
            return
        conn.execute(
            "INSERT INTO meeting_exception (meeting_id, original_start_utc, cancelled, title, "
            "start_utc, duration_min, link, reminder_offsets) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meeting_id,
                timeutil.to_db(original_start),
                int(previous.cancelled),
                previous.title,
                timeutil.to_db(previous.start) if previous.start else None,
                previous.duration_min,
                previous.link,
                json.dumps(list(previous.reminder_offsets))
                if previous.reminder_offsets is not None
                else None,
            ),
        )


def soft_delete(conn: Connection, meeting_id: int, now: datetime) -> None:
    with transaction(conn):
        get(conn, meeting_id)
        conn.execute(
            "UPDATE meeting SET deleted_at = ? WHERE id = ?", (timeutil.to_db(now), meeting_id)
        )


def undelete(conn: Connection, meeting_id: int) -> None:
    with transaction(conn):
        get(conn, meeting_id, include_deleted=True)
        conn.execute("UPDATE meeting SET deleted_at = NULL WHERE id = ?", (meeting_id,))


def purge_deleted(conn: Connection, deleted_before: datetime) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM meeting WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (timeutil.to_db(deleted_before),),
        )
        return cursor.rowcount


def start_adhoc(
    conn: Connection, draft: MeetingDraft, tzid: str, now: datetime, *, open_ended: bool
) -> tuple[int, Trim]:
    """Create an ad-hoc meeting, pause a running timer for it and remove timer time after
    its real start, all in one transaction (D-049). A manual entry after the start refuses
    it (ValidationError on "started")."""
    with transaction(conn):
        meeting_id = create(conn, draft, tzid, now, is_adhoc=True, open_ended=open_ended)
        start = get(conn, meeting_id).start
        trim = timer_repo.apply_trim(conn, start, now, occurrence_key(meeting_id, start))
        return meeting_id, trim


def end_adhoc(conn: Connection, meeting_id: int, duration_min: int, now: datetime) -> None:
    """Store the real length of an ad-hoc meeting and close it (PLAN §5.3)."""
    with transaction(conn):
        meeting = get(conn, meeting_id)
        if not meeting.is_adhoc:
            raise ValueError("only ad-hoc meetings are ended by hand")
        conn.execute(
            "UPDATE meeting SET duration_min = ?, open_ended = 0, updated_at = ? WHERE id = ?",
            (duration_min, timeutil.to_db(now), meeting_id),
        )


def reopen_adhoc(conn: Connection, meeting: Meeting) -> None:
    """Undo of ending an ad-hoc meeting: back to its previous length and openness."""
    with transaction(conn):
        conn.execute(
            "UPDATE meeting SET duration_min = ?, open_ended = ?, updated_at = ? WHERE id = ?",
            (
                meeting.duration_min,
                int(meeting.open_ended),
                timeutil.to_db(meeting.updated_at),
                meeting.id,
            ),
        )
