"""First-run choices (SPEC §7): working hours and days, lunch, a first project, optional
areas and sample data. Pure."""

import dataclasses
from dataclasses import dataclass
from datetime import time
from gettext import gettext as _

from daybook.core import settings as settings_rules
from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.core.models import ProjectDraft
from daybook.core.settings import Settings

SUGGESTED_AREAS = ("Docs", "Demos")


@dataclass(frozen=True)
class Choices:
    work_start: time = time(9, 0)
    work_end: time = time(18, 0)
    workdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    lunch_start: time = time(13, 0)
    lunch_minutes: int = 60
    project_name: str = ""  # empty: no first project
    key_prefix: str = ""
    url_template: str = ""
    areas: tuple[str, ...] = ()
    sample_data: bool = False


def needs_onboarding(done: bool, tasks: int, extra_projects: int, meetings: int) -> bool:
    """A first run is a database with the flag unset and nothing of the user's in it, so
    data from before this feature (or restored from a backup) never meets the dialog."""
    return not done and not (tasks or extra_projects or meetings)


def settings_for(current: Settings, choices: Choices) -> Settings:
    """The settings these choices make. Raises ValidationError naming the field."""
    result = dataclasses.replace(
        current,
        work_start=choices.work_start,
        work_end=choices.work_end,
        workdays=choices.workdays,
        lunch_start=choices.lunch_start,
        lunch_minutes=choices.lunch_minutes,
    )
    settings_rules.validate(result)
    return result


def project_draft(choices: Choices) -> ProjectDraft | None:
    """The first project, if one was named; an area when it has no prefix or link."""
    if not choices.project_name.strip():
        if choices.key_prefix.strip() or choices.url_template.strip():
            raise ValidationError("project_name", _("Give the project a name."))
        return None
    return validation.project_draft(
        ProjectDraft(
            name=choices.project_name,
            key_prefix=choices.key_prefix or None,
            url_template=choices.url_template or None,
        )
    )


def area_names(choices: Choices) -> tuple[str, ...]:
    """The extra areas to create: valid names, without repeats, and without clashing with
    the first project or General."""
    seen = {"general", choices.project_name.strip().casefold()}
    names: list[str] = []
    for raw in choices.areas:
        name = validation.project_name(raw)
        if name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return tuple(names)
