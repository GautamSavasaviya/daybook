"""Meeting rules: validation, occurrences with single-occurrence changes, ad-hoc meetings.

Pure: the repository loads meetings and exceptions, these functions turn them into the
occurrences that the timeline, the planner and (Phase 4) the reminders use.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daybook.core import recurrence, validation
from daybook.core.models import (
    Meeting,
    MeetingDraft,
    MeetingException,
    Occurrence,
    OccurrenceEdit,
    Recurrence,
)

_ONE_SECOND = timedelta(seconds=1)
# An open "Meeting now" with unknown length always reaches at least a minute past now,
# so the plan keeps moving while it lasts (PLAN §5.3).
OPEN_MEETING_LEAD = timedelta(minutes=1)
# "Started … ago" choices for Meeting now (SPEC A7).
MAX_STARTED_AGO_MIN = 30


def zone(tzid: str, fallback: tzinfo = UTC) -> tzinfo:
    """The meeting's own zone (D-010). A zone that can't be loaded (an unnamed system
    zone, or one removed from tzdata) falls back to the caller's local zone."""
    try:
        return ZoneInfo(tzid)
    except ZoneInfoNotFoundError, ValueError:
        return fallback


def validate_draft(draft: MeetingDraft) -> MeetingDraft:
    if draft.start.tzinfo is None:
        raise ValueError("meeting start must be timezone-aware")
    return MeetingDraft(
        title=validation.title(draft.title),
        start=draft.start.astimezone(UTC).replace(second=0, microsecond=0),
        duration_min=validation.meeting_duration(draft.duration_min),
        link=validation.link(draft.link),
        recurrence=recurrence.validate(draft.recurrence) if draft.recurrence else None,
        reminder_offsets=validation.reminder_offsets(draft.reminder_offsets),
        project_id=draft.project_id,
    )


def validate_edit(edit: OccurrenceEdit) -> OccurrenceEdit:
    return OccurrenceEdit(
        title=validation.title(edit.title),
        start=edit.start.astimezone(UTC).replace(second=0, microsecond=0),
        duration_min=validation.meeting_duration(edit.duration_min),
        link=validation.link(edit.link),
        reminder_offsets=validation.reminder_offsets(edit.reminder_offsets),
    )


def _base(meeting: Meeting, original: datetime) -> Occurrence:
    return Occurrence(
        meeting_id=meeting.id,
        original_start=original,
        start=original,
        duration_min=meeting.duration_min,
        title=meeting.title,
        link=meeting.link,
        reminder_offsets=meeting.reminder_offsets,
        project_id=meeting.project_id,
        recurring=meeting.recurrence is not None,
        is_adhoc=meeting.is_adhoc,
        open_ended=meeting.open_ended,
    )


def _apply(occurrence: Occurrence, change: MeetingException) -> Occurrence:
    return Occurrence(
        meeting_id=occurrence.meeting_id,
        original_start=occurrence.original_start,
        start=change.start or occurrence.start,
        duration_min=change.duration_min or occurrence.duration_min,
        title=change.title or occurrence.title,
        link=change.link if change.link is not None else occurrence.link,
        reminder_offsets=(
            change.reminder_offsets
            if change.reminder_offsets is not None
            else occurrence.reminder_offsets
        ),
        project_id=occurrence.project_id,
        recurring=occurrence.recurring,
        is_adhoc=occurrence.is_adhoc,
        open_ended=occurrence.open_ended,
        edited=True,
    )


def is_occurrence(meeting: Meeting, original: datetime, fallback: tzinfo = UTC) -> bool:
    """Whether the series really has an occurrence starting at `original`."""
    return bool(
        recurrence.starts(
            meeting.recurrence,
            meeting.start,
            zone(meeting.tzid, fallback),
            original,
            original + _ONE_SECOND,
        )
    )


def occurrences(
    meeting: Meeting,
    exceptions: Sequence[MeetingException],
    window_start: datetime,
    window_end: datetime,
    fallback: tzinfo = UTC,
) -> list[Occurrence]:
    """Occurrences starting in [window_start, window_end), with single-occurrence changes
    applied: cancelled ones are dropped, moved ones appear at their new time, even when
    the original time is outside the window (PLAN §5.4)."""
    changes = {change.original_start: change for change in exceptions}
    originals = recurrence.starts(
        meeting.recurrence, meeting.start, zone(meeting.tzid, fallback), window_start, window_end
    )
    result: list[Occurrence] = []
    for original in originals:
        change = changes.get(original)
        occurrence = _base(meeting, original)
        if change is not None:
            if change.cancelled:
                continue
            occurrence = _apply(occurrence, change)
        if window_start <= occurrence.start < window_end:
            result.append(occurrence)
    in_window = set(originals)
    for change in exceptions:
        moved_in = (
            not change.cancelled
            and change.start is not None
            and change.original_start not in in_window
            and window_start <= change.start < window_end
        )
        if moved_in and is_occurrence(meeting, change.original_start, fallback):
            result.append(_apply(_base(meeting, change.original_start), change))
    result.sort(key=lambda o: (o.start, o.meeting_id))
    return result


def orphaned_exceptions(
    new_start: datetime,
    new_rule: Recurrence | None,
    meeting: Meeting,
    exceptions: Sequence[MeetingException],
    fallback: tzinfo = UTC,
) -> list[MeetingException]:
    """Single-occurrence changes that no longer match an occurrence after the series'
    start or rule changes; they are discarded after a confirmation (D-013)."""
    if new_start == meeting.start and new_rule == meeting.recurrence:
        return []
    tz = zone(meeting.tzid, fallback)
    return [
        change
        for change in exceptions
        if not recurrence.starts(
            new_rule, new_start, tz, change.original_start, change.original_start + _ONE_SECOND
        )
    ]


def effective_end(occurrence: Occurrence, now: datetime) -> datetime:
    """When an occurrence ends for planning: an open-ended ad-hoc meeting keeps going."""
    if occurrence.open_ended:
        return max(occurrence.end, now + OPEN_MEETING_LEAD)
    return occurrence.end


def ended_duration(start: datetime, now: datetime) -> int:
    """The real length of a meeting ended now, in whole minutes (at least 1). Rounded
    down, so the meeting is over as soon as it is ended."""
    return max(1, int((now - start).total_seconds() // 60))


def in_progress(occurrence: Occurrence, now: datetime) -> bool:
    return occurrence.start <= now < effective_end(occurrence, now)
