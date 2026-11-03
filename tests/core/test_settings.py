import dataclasses
import json
from datetime import time

import pytest

from daybook.core import settings as model
from daybook.core.errors import ValidationError
from daybook.core.repo import app_state
from daybook.core.repo import settings as repo
from daybook.core.settings import Settings


def test_defaults_match_the_spec():
    s = Settings()
    assert (s.work_start, s.work_end) == (time(9), time(18))
    assert s.workdays == frozenset({0, 1, 2, 3, 4})
    assert s.meeting_offsets == (10, 1)
    assert s.allotment_warning_min == 10
    assert s.timesheet_rounding_min == 15
    assert s.idle_threshold_min == 10
    assert s.run_in_background is True
    assert s.sound_enabled is True
    assert s.auto_pause_meetings is True
    assert s.start_at_login is False
    assert s.auto_carry_over is True
    assert s.meeting_reminders_on_leave is False
    model.validate(s)


def test_rows_round_trip():
    custom = Settings(
        work_start=time(8, 30),
        workdays=frozenset({0, 2, 4}),
        meeting_offsets=(15, 5, 1),
        sound_enabled=False,
    )
    rows = model.to_rows(custom)
    assert rows["work_start"] == '"08:30"'
    assert json.loads(rows["workdays"]) == [0, 2, 4]
    assert model.from_rows(rows) == custom


def test_unknown_missing_and_damaged_rows_fall_back_to_defaults():
    rows = {
        "no_such_setting": "1",
        "lunch_minutes": "not json",
        "sound_enabled": '"yes"',
        "allotment_warning_min": "true",
        "work_start": '"9:00"',
        "meeting_offsets": '"10"',
        "workdays": "[0, 1.5]",
    }
    assert model.from_rows(rows) == Settings()


def test_inconsistent_work_hours_are_repaired_as_a_group():
    rows = model.to_rows(Settings(work_start=time(10), work_end=time(19), lunch_start=time(14)))
    rows["work_end"] = '"08:00"'  # now ends before it starts
    loaded = model.from_rows(rows)
    defaults = Settings()
    assert (loaded.work_start, loaded.work_end, loaded.lunch_start) == (
        defaults.work_start,
        defaults.work_end,
        defaults.lunch_start,
    )


def test_unrepairable_rows_give_full_defaults(monkeypatch):
    rows = model.to_rows(Settings(work_start=time(10), work_end=time(9)))

    def always_invalid(_settings):
        raise ValidationError("workdays", "nope")

    monkeypatch.setattr(model, "validate", always_invalid)
    assert model.from_rows(rows) == Settings()


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"work_end": time(9)}, "work_end"),
        ({"lunch_start": time(8)}, "lunch_start"),
        ({"lunch_start": time(17, 30)}, "lunch_start"),
        ({"lunch_minutes": 300}, "lunch_minutes"),
        ({"workdays": frozenset()}, "workdays"),
        ({"workdays": frozenset({7})}, "workdays"),
        ({"meeting_offsets": ()}, "meeting_offsets"),
        ({"meeting_offsets": (5, 5)}, "meeting_offsets"),
        ({"meeting_offsets": (-1,)}, "meeting_offsets"),
        ({"allotment_warning_min": 0}, "allotment_warning_min"),
        ({"drift_after_min": 500}, "drift_after_min"),
        ({"timesheet_rounding_min": 7}, "timesheet_rounding_min"),
        ({"idle_threshold_min": 0}, "idle_threshold_min"),
        ({"adhoc_default_min": 1}, "adhoc_default_min"),
    ],
)
def test_validation_names_the_bad_field(changes, field):
    with pytest.raises(ValidationError) as info:
        model.validate(dataclasses.replace(Settings(), **changes))
    assert info.value.field == field
    assert info.value.message


def test_lunch_can_be_disabled_anywhere():
    model.validate(Settings(lunch_minutes=0, lunch_start=time(23, 0)))


def test_repo_load_save_round_trip(conn):
    assert repo.load(conn) == Settings()
    changed = Settings(work_end=time(17), timesheet_rounding_min=30)
    repo.save(conn, changed)
    assert repo.load(conn) == changed


def test_repo_save_rejects_invalid_settings_without_writing(conn):
    with pytest.raises(ValidationError):
        repo.save(conn, Settings(work_end=time(8)))
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] == 0


def test_app_state_get_put_delete(conn):
    assert app_state.get(conn, "last_check_utc") is None
    assert app_state.get(conn, "missing", default=3) == 3
    app_state.put(conn, "last_check_utc", "2026-11-03T08:00:00Z")
    app_state.put(conn, "onboarding", {"done": True})
    app_state.put(conn, "onboarding", {"done": False})
    assert app_state.get(conn, "last_check_utc") == "2026-11-03T08:00:00Z"
    assert app_state.get(conn, "onboarding") == {"done": False}
    app_state.delete(conn, "onboarding")
    assert app_state.get(conn, "onboarding") is None
