from datetime import time

import pytest

from daybook.core import onboarding
from daybook.core.errors import ValidationError
from daybook.core.onboarding import Choices
from daybook.core.settings import Settings


@pytest.mark.parametrize(
    ("done", "tasks", "projects", "meetings", "expected"),
    [
        (False, 0, 0, 0, True),  # a fresh database
        (True, 0, 0, 0, False),  # already answered or skipped
        (False, 3, 0, 0, False),  # data from before: never ask
        (False, 0, 1, 0, False),
        (False, 0, 0, 2, False),
    ],
)
def test_who_sees_the_first_run(done, tasks, projects, meetings, expected):
    assert onboarding.needs_onboarding(done, tasks, projects, meetings) is expected


def test_settings_for_applies_the_choices_and_validates():
    choices = Choices(
        work_start=time(8, 30),
        work_end=time(17, 0),
        workdays=frozenset({0, 1, 2, 3}),
        lunch_start=time(12, 0),
        lunch_minutes=45,
    )
    result = onboarding.settings_for(Settings(sound_enabled=False), choices)
    assert (result.work_start, result.work_end) == (time(8, 30), time(17, 0))
    assert result.workdays == frozenset({0, 1, 2, 3})
    assert (result.lunch_start, result.lunch_minutes) == (time(12, 0), 45)
    assert result.sound_enabled is False  # everything else is kept
    for bad, field in (
        (Choices(work_start=time(18, 0), work_end=time(9, 0)), "work_end"),
        (Choices(workdays=frozenset()), "workdays"),
        (Choices(lunch_start=time(8, 0)), "lunch_start"),
    ):
        with pytest.raises(ValidationError) as caught:
            onboarding.settings_for(Settings(), bad)
        assert caught.value.field == field


def test_the_first_project_is_optional_and_checked():
    assert onboarding.project_draft(Choices()) is None
    draft = onboarding.project_draft(
        Choices(project_name=" PMS ", key_prefix="proj", url_template="https://x/{key}")
    )
    assert draft is not None
    assert (draft.name, draft.key_prefix) == ("PMS", "PROJ")
    area = onboarding.project_draft(Choices(project_name="Docs"))
    assert area is not None
    assert area.key_prefix is None
    with pytest.raises(ValidationError) as caught:
        onboarding.project_draft(Choices(key_prefix="PROJ"))  # a prefix with no name
    assert caught.value.field == "project_name"
    with pytest.raises(ValidationError):
        onboarding.project_draft(Choices(project_name="PMS", key_prefix="not a prefix!"))


def test_areas_are_cleaned_and_never_repeat_the_project_or_general():
    choices = Choices(project_name="Docs", areas=("Docs", "Demos", " demos ", "General", "Ops"))
    assert onboarding.area_names(choices) == ("Demos", "Ops")
    assert onboarding.area_names(Choices()) == ()
