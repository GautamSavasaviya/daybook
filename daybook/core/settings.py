"""User settings: a frozen dataclass with defaults, stored one JSON value per key.

Loading is lenient: unknown keys are ignored and missing or invalid values fall back to
their defaults (logged), so a damaged row can never stop the app from starting. Saving is
strict: `validate()` runs first and raises `ValidationError` naming the field.
"""

import dataclasses
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import time
from gettext import gettext as _
from types import GenericAlias
from typing import Any

from daybook.core.errors import ValidationError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    work_start: time = time(9, 0)
    work_end: time = time(18, 0)
    workdays: frozenset[int] = field(default_factory=lambda: frozenset({0, 1, 2, 3, 4}))
    lunch_start: time = time(13, 0)
    lunch_minutes: int = 60
    meeting_offsets: tuple[int, ...] = (10, 1)
    allotment_warning_min: int = 10
    drift_after_min: int = 5
    sound_enabled: bool = True
    auto_pause_meetings: bool = True
    timesheet_rounding_min: int = 15
    start_at_login: bool = False
    idle_threshold_min: int = 10
    run_in_background: bool = True
    auto_carry_over: bool = True
    meeting_reminders_on_leave: bool = False
    adhoc_default_min: int = 30


def _encode_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _decode_time(raw: object) -> time:
    if not isinstance(raw, str):
        raise TypeError("expected HH:MM")
    hours, minutes = raw.split(":")
    if len(hours) != 2 or len(minutes) != 2:
        raise ValueError("expected HH:MM")
    return time(int(hours), int(minutes))


def _decode_int(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError("expected an integer")
    return raw


def _decode_bool(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise TypeError("expected true or false")
    return raw


def _decode_int_list(raw: object) -> list[int]:
    if not isinstance(raw, list):
        raise TypeError("expected a list")
    return [_decode_int(item) for item in raw]


_Codec = tuple[Callable[[Any], Any], Callable[[Any], Any]]
_CODECS: dict[str, _Codec] = {
    "time": (_encode_time, _decode_time),
    "int": (lambda v: v, _decode_int),
    "bool": (lambda v: v, _decode_bool),
    "frozenset[int]": (sorted, lambda raw: frozenset(_decode_int_list(raw))),
    "tuple[int, ...]": (list, lambda raw: tuple(_decode_int_list(raw))),
}


def _codec(f: dataclasses.Field[Any]) -> _Codec:
    # Plain classes are keyed by name ("time", "int"); generics by their repr ("tuple[int, ...]").
    if isinstance(f.type, str):
        return _CODECS[f.type]
    if isinstance(f.type, GenericAlias):
        return _CODECS[str(f.type)]
    return _CODECS[f.type.__name__]


FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Settings))


def to_rows(settings: Settings) -> dict[str, str]:
    """Serialise every setting to its stored JSON text."""
    rows: dict[str, str] = {}
    for f in dataclasses.fields(Settings):
        encode, _decode = _codec(f)
        rows[f.name] = json.dumps(encode(getattr(settings, f.name)))
    return rows


def from_rows(rows: Mapping[str, str]) -> Settings:
    """Build settings from stored rows; bad or missing values use the defaults."""
    values: dict[str, Any] = {}
    for f in dataclasses.fields(Settings):
        if f.name not in rows:
            continue
        _encode, decode = _codec(f)
        try:
            values[f.name] = decode(json.loads(rows[f.name]))
        except (ValueError, TypeError) as exc:
            log.warning("ignoring stored setting %s=%r: %s", f.name, rows[f.name], exc)
    candidate = Settings(**values)
    try:
        validate(candidate)
    except ValidationError as exc:
        log.warning("stored settings are inconsistent (%s); using defaults for that group", exc)
        return _repair(candidate, exc.field)
    return candidate


def _repair(settings: Settings, bad_field: str) -> Settings:
    defaults = Settings()
    group = _GROUPS.get(bad_field, (bad_field,))
    repaired = dataclasses.replace(settings, **{name: getattr(defaults, name) for name in group})
    try:
        validate(repaired)
    except ValidationError:
        return defaults
    return repaired


# Fields validated together: repairing one of them restores the whole group.
_WORK_HOURS = ("work_start", "work_end", "lunch_start", "lunch_minutes")
_GROUPS: dict[str, tuple[str, ...]] = dict.fromkeys(_WORK_HOURS, _WORK_HOURS)


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValidationError(
            name, _("Must be between {low} and {high}.").format(low=low, high=high)
        )


def validate(settings: Settings) -> None:
    """Raise `ValidationError` for the first invalid field."""
    start, end = _minutes(settings.work_start), _minutes(settings.work_end)
    if end <= start:
        raise ValidationError("work_end", _("Work must end after it starts."))
    _check_range("lunch_minutes", settings.lunch_minutes, 0, 240)
    if settings.lunch_minutes:
        lunch = _minutes(settings.lunch_start)
        if lunch < start or lunch + settings.lunch_minutes > end:
            raise ValidationError("lunch_start", _("Lunch must fall within working hours."))
    if not settings.workdays:
        raise ValidationError("workdays", _("Choose at least one workday."))
    if not settings.workdays <= frozenset(range(7)):
        raise ValidationError("workdays", _("Workdays must be Monday to Sunday."))
    if not settings.meeting_offsets:
        raise ValidationError("meeting_offsets", _("Add at least one reminder time."))
    if len(set(settings.meeting_offsets)) != len(settings.meeting_offsets):
        raise ValidationError("meeting_offsets", _("Reminder times must be different."))
    for offset in settings.meeting_offsets:
        _check_range("meeting_offsets", offset, 0, 24 * 60)
    _check_range("allotment_warning_min", settings.allotment_warning_min, 1, 120)
    _check_range("drift_after_min", settings.drift_after_min, 1, 120)
    if settings.timesheet_rounding_min not in (0, 5, 6, 10, 15, 30, 60):
        raise ValidationError(
            "timesheet_rounding_min", _("Choose off, 5, 6, 10, 15, 30 or 60 minutes.")
        )
    _check_range("idle_threshold_min", settings.idle_threshold_min, 1, 240)
    _check_range("adhoc_default_min", settings.adhoc_default_min, 5, 480)
