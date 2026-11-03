"""Field validators shared by the repositories and the forms.

Each validator returns the normalised value or raises `ValidationError(field, message)`,
where `field` is the name the form uses to show the message inline.
"""

import re
from collections.abc import Sequence
from datetime import date
from gettext import gettext as _

from daybook.core.errors import ValidationError
from daybook.core.models import Priority, ProjectColor, ProjectDraft, TaskDraft

MAX_TITLE = 500
MAX_NAME = 80
MAX_PREFIX = 10
MAX_TICKET = 64
MAX_REQUESTED_BY = 80
MAX_TAG = 40
MAX_NOTES = 100_000
MAX_SHORT_TEXT = 500
MAX_ESTIMATE_MIN = 999 * 60
MAX_ALLOTMENT_MIN = 24 * 60

PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# A PMS-style key such as PROJ-123; its prefix links it to a project (D-018).
TICKET_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)-(\d+)")
_DURATION_RE = re.compile(
    r"(?:(?P<hours>\d+(?:[.,]\d+)?)\s*h)?\s*(?:(?P<minutes>\d+)\s*m(?:in)?)?", re.IGNORECASE
)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def title(value: str, field: str = "title") -> str:
    cleaned = _collapse(value)
    if not cleaned:
        raise ValidationError(field, _("Enter a title."))
    if len(cleaned) > MAX_TITLE:
        raise ValidationError(field, _("Keep it under {count} characters.").format(count=MAX_TITLE))
    return cleaned


def optional_text(value: str | None, field: str, max_length: int = MAX_SHORT_TEXT) -> str | None:
    """Collapse whitespace; an empty value becomes None."""
    if value is None:
        return None
    cleaned = _collapse(value)
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise ValidationError(
            field, _("Keep it under {count} characters.").format(count=max_length)
        )
    return cleaned


def notes(value: str) -> str:
    # Notes keep their line breaks; only trailing whitespace is trimmed.
    cleaned = value.rstrip()
    if len(cleaned) > MAX_NOTES:
        raise ValidationError("notes", _("Notes are too long."))
    return cleaned


def project_name(value: str) -> str:
    cleaned = _collapse(value)
    if not cleaned:
        raise ValidationError("name", _("Enter a name."))
    if len(cleaned) > MAX_NAME:
        raise ValidationError("name", _("Keep it under {count} characters.").format(count=MAX_NAME))
    return cleaned


def key_prefix(value: str | None) -> str | None:
    """Letters and digits, starting with a letter; stored upper-case (D-018)."""
    if value is None or not value.strip():
        return None
    cleaned = value.strip()
    if not PREFIX_RE.fullmatch(cleaned):
        raise ValidationError(
            "key_prefix", _("Use letters and digits only, starting with a letter (e.g. PROJ).")
        )
    if len(cleaned) > MAX_PREFIX:
        raise ValidationError(
            "key_prefix", _("Keep it under {count} characters.").format(count=MAX_PREFIX)
        )
    return cleaned.upper()


def url_template(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    cleaned = value.strip()
    if not cleaned.lower().startswith(("https://", "http://")):
        raise ValidationError("url_template", _("Start with https:// or http://."))
    if "{key}" not in cleaned:
        raise ValidationError(
            "url_template",
            _("Include {key} where the ticket key goes, e.g. https://pms.example.com/browse/{key}"),
        )
    if any(ch.isspace() for ch in cleaned):
        raise ValidationError("url_template", _("A link can't contain spaces."))
    return cleaned


def project_draft(draft: ProjectDraft) -> ProjectDraft:
    return ProjectDraft(
        name=project_name(draft.name),
        color=ProjectColor(draft.color),
        key_prefix=key_prefix(draft.key_prefix),
        url_template=url_template(draft.url_template),
    )


def ticket_key(value: str | None) -> str | None:
    """Any key without spaces; PMS-style keys (PROJ-123) are upper-cased."""
    if value is None or not value.strip():
        return None
    cleaned = value.strip()
    if any(ch.isspace() for ch in cleaned):
        raise ValidationError("ticket_key", _("A ticket key can't contain spaces."))
    if len(cleaned) > MAX_TICKET:
        raise ValidationError(
            "ticket_key", _("Keep it under {count} characters.").format(count=MAX_TICKET)
        )
    return cleaned.upper() if TICKET_RE.fullmatch(cleaned) else cleaned


def ticket_prefix(key: str) -> str | None:
    match = TICKET_RE.fullmatch(key)
    return match.group(1).upper() if match else None


def priority(value: int) -> Priority:
    try:
        return Priority(value)
    except ValueError:
        raise ValidationError("priority", _("Choose P1 to P4.")) from None


def estimate(value: int | None) -> int | None:
    if value is None:
        return None
    if not 0 < value <= MAX_ESTIMATE_MIN:
        raise ValidationError("estimate", _("Enter a time between 1 minute and 999 hours."))
    return value


def allotment(value: int | None) -> int | None:
    """Minutes planned for one day; None is "Rest of day"."""
    if value is None:
        return None
    if not 0 < value <= MAX_ALLOTMENT_MIN:
        raise ValidationError("allotment", _("Enter a time between 1 minute and 24 hours."))
    return value


def tag_name(value: str) -> str:
    cleaned = value.strip().lstrip("#").strip()
    if not cleaned:
        raise ValidationError("tags", _("A tag can't be empty."))
    if any(ch.isspace() for ch in cleaned) or "," in cleaned:
        raise ValidationError("tags", _("A tag is a single word."))
    if len(cleaned) > MAX_TAG:
        raise ValidationError(
            "tags", _("Keep tags under {count} characters.").format(count=MAX_TAG)
        )
    return cleaned


def tag_names(values: Sequence[str]) -> tuple[str, ...]:
    """Validate and de-duplicate (case-insensitively), keeping the first spelling."""
    seen: dict[str, str] = {}
    for value in values:
        name = tag_name(value)
        seen.setdefault(name.casefold(), name)
    return tuple(seen.values())


def split_tags(text: str) -> tuple[str, ...]:
    """Parse "backend, ui #urgent" into tag names."""
    parts = re.split(r"[\s,]+", text)
    return tag_names([part for part in parts if part.strip().lstrip("#")])


def checklist_text(value: str) -> str:
    cleaned = _collapse(value)
    if not cleaned:
        raise ValidationError("checklist", _("Enter some text."))
    if len(cleaned) > MAX_SHORT_TEXT:
        raise ValidationError(
            "checklist", _("Keep it under {count} characters.").format(count=MAX_SHORT_TEXT)
        )
    return cleaned


def task_draft(draft: TaskDraft) -> TaskDraft:
    return TaskDraft(
        title=title(draft.title),
        project_id=draft.project_id,
        priority=priority(draft.priority),
        ticket_key=ticket_key(draft.ticket_key),
        requested_by=optional_text(draft.requested_by, "requested_by", MAX_REQUESTED_BY),
        estimate_min=estimate(draft.estimate_min),
        due_date=draft.due_date,
        notes=notes(draft.notes),
        tags=tag_names(draft.tags),
        plan=draft.plan,
    )


def parse_duration(text: str, field: str = "estimate") -> int | None:
    """Parse ``2h``, ``30m``, ``1h30m``, ``1h 30m``, ``1.5h`` or a bare number of minutes.

    Returns minutes, or None for empty input. Raises ValidationError for anything else.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        minutes = int(cleaned)
    else:
        match = _DURATION_RE.fullmatch(cleaned)
        if not match or not (match.group("hours") or match.group("minutes")):
            raise ValidationError(field, _("Use a time like 30m, 2h or 1h 30m."))
        hours = float((match.group("hours") or "0").replace(",", "."))
        minutes = round(hours * 60) + int(match.group("minutes") or 0)
    if minutes <= 0:
        raise ValidationError(field, _("Enter a time longer than zero."))
    return minutes


def follow_up_date(value: date | None, today: date) -> date | None:
    if value is not None and value < today:
        raise ValidationError("follow_up", _("Pick today or a later date."))
    return value
