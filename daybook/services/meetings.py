"""Meetings: series, single-occurrence changes and "Meeting now" (PLAN §5.3, §5.4)."""

import logging
from datetime import date, datetime, timedelta
from gettext import gettext as _

from daybook.core import meetings as rules
from daybook.core import timeutil, validation
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import NotFoundError, ValidationError
from daybook.core.models import Meeting, MeetingDraft, Occurrence, OccurrenceEdit
from daybook.core.repo import meetings as meetings_repo
from daybook.core.timeutil import fmt_day
from daybook.services.events import Changed, Entity, EventBus, Toast
from daybook.services.settings import SettingsService
from daybook.services.text import short

log = logging.getLogger(__name__)

PURGE_AFTER = timedelta(hours=24)
# Looking this far back finds meetings that started earlier and are still running.
_LONGEST_MEETING = timedelta(hours=24)


class MeetingService:
    def __init__(
        self, conn: Connection, bus: EventBus, clock: Clock, settings: SettingsService
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock
        self._settings = settings

    # Reading ----------------------------------------------------------------------------

    def get(self, meeting_id: int) -> Meeting:
        return meetings_repo.get(self._conn, meeting_id)

    def all(self) -> list[Meeting]:
        return meetings_repo.active(self._conn)

    def default_offsets(self) -> tuple[int, ...]:
        return self._settings.current.meeting_offsets

    def occurrences(self, start: datetime, end: datetime) -> list[Occurrence]:
        """Every occurrence starting in [start, end), single-occurrence changes applied."""
        changes = meetings_repo.exceptions(self._conn)
        result: list[Occurrence] = []
        for meeting in meetings_repo.active(self._conn):
            result += rules.occurrences(
                meeting, changes.get(meeting.id, []), start, end, self._clock.local_tz()
            )
        result.sort(key=lambda o: (o.start, o.meeting_id))
        return result

    def day_occurrences(self, day: date) -> list[Occurrence]:
        """Occurrences that overlap the local day, including ones still running from
        before midnight."""
        start, end = timeutil.local_day_bounds(day, self._clock.local_tz())
        now = self._clock.now()
        return [
            o
            for o in self.occurrences(start - _LONGEST_MEETING, end)
            if rules.effective_end(o, now) > start
        ]

    def occurrence(self, meeting_id: int, original_start: datetime) -> Occurrence | None:
        meeting = self.get(meeting_id)
        changes = meetings_repo.exceptions(self._conn, meeting_id).get(meeting_id, [])
        found = rules.occurrences(
            meeting,
            changes,
            original_start,
            original_start + timedelta(seconds=1),
            self._clock.local_tz(),
        )
        moved = [
            rules.occurrences(
                meeting, changes, c.start, c.start + timedelta(seconds=1), self._clock.local_tz()
            )
            for c in changes
            if c.original_start == original_start and c.start is not None
        ]
        for candidate in [*found, *(o for group in moved for o in group)]:
            if candidate.original_start == original_start:
                return candidate
        return None

    def in_progress(self) -> list[Occurrence]:
        now = self._clock.now()
        return [
            o
            for o in self.occurrences(now - _LONGEST_MEETING, now + timedelta(seconds=1))
            if rules.in_progress(o, now)
        ]

    def open_adhoc(self) -> list[Occurrence]:
        """ "Meeting now" meetings that are running (open-ended or not yet over)."""
        return [o for o in self.in_progress() if o.is_adhoc]

    # Series -----------------------------------------------------------------------------

    def create(self, draft: MeetingDraft) -> int:
        meeting_id = meetings_repo.create(
            self._conn, draft, str(self._clock.local_tz()), self._clock.now()
        )
        self._changed()
        return meeting_id

    def discarded_by(self, meeting_id: int, draft: MeetingDraft) -> int:
        """How many single-occurrence changes a series edit would discard (D-013)."""
        meeting = self.get(meeting_id)
        clean = rules.validate_draft(draft)
        changes = meetings_repo.exceptions(self._conn, meeting_id).get(meeting_id, [])
        return len(
            rules.orphaned_exceptions(
                clean.start, clean.recurrence, meeting, changes, self._clock.local_tz()
            )
        )

    def update_series(self, meeting_id: int, draft: MeetingDraft) -> None:
        """Change every occurrence. Changes that no longer match an occurrence are
        discarded; the UI confirms first with `discarded_by`."""
        meeting = self.get(meeting_id)
        clean = rules.validate_draft(draft)
        changes = meetings_repo.exceptions(self._conn, meeting_id).get(meeting_id, [])
        orphans = rules.orphaned_exceptions(
            clean.start, clean.recurrence, meeting, changes, self._clock.local_tz()
        )
        meetings_repo.update_series(
            self._conn, meeting_id, clean, self._clock.now(), [c.id for c in orphans]
        )
        self._changed()

    def delete(self, meeting_id: int) -> None:
        meeting = self.get(meeting_id)
        meetings_repo.soft_delete(self._conn, meeting_id, self._clock.now())
        self._changed()
        self._bus.publish(
            Toast(
                _("Deleted “{title}”").format(title=short(meeting.title)),
                undo=lambda: self._undo_delete(meeting_id),
            )
        )

    def _undo_delete(self, meeting_id: int) -> None:
        try:
            meetings_repo.undelete(self._conn, meeting_id)
        except NotFoundError:
            self._bus.publish(Toast(_("Couldn't undo: the meeting no longer exists")))
            return
        self._changed()

    def purge_deleted(self) -> int:
        return meetings_repo.purge_deleted(self._conn, self._clock.now() - PURGE_AFTER)

    # One occurrence ---------------------------------------------------------------------

    def edit_occurrence(
        self, meeting_id: int, original_start: datetime, edit: OccurrenceEdit
    ) -> None:
        meetings_repo.set_occurrence(
            self._conn,
            self.get(meeting_id),
            original_start,
            edit=edit,
            fallback=self._clock.local_tz(),
        )
        self._changed()

    def cancel_occurrence(self, meeting_id: int, original_start: datetime) -> None:
        meeting = self.get(meeting_id)
        previous = meetings_repo.exception_for(self._conn, meeting_id, original_start)
        meetings_repo.set_occurrence(
            self._conn, meeting, original_start, cancelled=True, fallback=self._clock.local_tz()
        )
        self._changed()
        today = self._clock.now().astimezone(self._clock.local_tz()).date()
        day = original_start.astimezone(self._clock.local_tz()).date()

        def undo() -> None:
            try:
                meetings_repo.restore_exception(self._conn, meeting_id, original_start, previous)
            except NotFoundError:
                self._bus.publish(Toast(_("Couldn't undo: the meeting no longer exists")))
                return
            self._changed()

        self._bus.publish(
            Toast(
                _("Cancelled “{title}” on {day}").format(
                    title=short(meeting.title), day=fmt_day(day, today)
                ),
                undo=undo,
            )
        )

    # Meeting now ------------------------------------------------------------------------

    def start_now(
        self,
        title: str,
        link: str | None,
        duration_min: int | None,
        started_ago_min: int = 0,
    ) -> int:
        """Start an ad-hoc meeting (SPEC A7). No length means "Unknown": it stays open
        until ended, with a provisional length (D-049)."""
        if not 0 <= started_ago_min <= rules.MAX_STARTED_AGO_MIN:
            raise ValidationError("started", _("It can have started up to 30 minutes ago."))
        now = self._clock.now().replace(second=0, microsecond=0)
        draft = MeetingDraft(
            title=title.strip() or _("Meeting"),
            start=now - timedelta(minutes=started_ago_min),
            duration_min=duration_min or self._settings.current.adhoc_default_min,
            link=validation.link(link),
            reminder_offsets=(),
        )
        meeting_id = meetings_repo.create(
            self._conn,
            draft,
            str(self._clock.local_tz()),
            self._clock.now(),
            is_adhoc=True,
            open_ended=duration_min is None,
        )
        self._changed()
        return meeting_id

    def end_now(self, meeting_id: int) -> None:
        """End an ad-hoc meeting now and keep its real length, with Undo."""
        meeting = self.get(meeting_id)
        length = rules.ended_duration(meeting.start, self._clock.now())
        meetings_repo.end_adhoc(self._conn, meeting_id, length, self._clock.now())
        self._changed()

        def undo() -> None:
            meetings_repo.reopen_adhoc(self._conn, meeting)
            self._changed()

        self._bus.publish(
            Toast(
                _("Ended “{title}” ({length})").format(
                    title=short(meeting.title), length=timeutil.fmt_duration(length)
                ),
                undo=undo,
            )
        )

    def _changed(self) -> None:
        self._bus.publish(Changed(Entity.MEETING))
