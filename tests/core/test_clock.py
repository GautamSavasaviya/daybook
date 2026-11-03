from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from daybook.core import clock as clock_module
from daybook.core.clock import (
    ManualClock,
    ScaledClock,
    SystemClock,
    clock_from_env,
    resolve_local_zone,
)

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
SYSTEM_BERLIN = Path("/usr/share/zoneinfo/Europe/Berlin")


class FakeMonotonic:
    def __init__(self) -> None:
        self.value = 500.0

    def __call__(self) -> float:
        return self.value


def utc_zone() -> ZoneInfo:
    return ZoneInfo("UTC")


# --- SystemClock ----------------------------------------------------------------------


def test_system_clock_returns_aware_utc():
    clock = SystemClock(zone_resolver=utc_zone)
    now = clock.now()
    assert now.tzinfo is UTC
    assert clock.real_seconds_until(now - timedelta(minutes=1)) == 0.0
    assert 3590 < clock.real_seconds_until(now + timedelta(hours=1)) <= 3600
    assert clock.monotonic() <= clock.monotonic()


def test_system_clock_reports_timezone_changes(monkeypatch):
    zones = iter([ZoneInfo("UTC"), ZoneInfo("UTC"), BERLIN])
    monkeypatch.setattr(clock_module.time, "tzset", lambda: None)
    clock = SystemClock(zone_resolver=lambda: next(zones))
    assert clock.refresh_local_tz() is False
    assert clock.refresh_local_tz() is True
    assert clock.local_tz() == BERLIN


# --- ScaledClock ----------------------------------------------------------------------


def test_scaled_clock_runs_faster_from_the_fake_start():
    mono = FakeMonotonic()
    clock = ScaledClock(60, START, monotonic=mono, zone_resolver=utc_zone)
    assert clock.now() == START
    mono.value += 2  # two real seconds = two minutes
    assert clock.now() == START + timedelta(minutes=2)
    assert clock.real_seconds_until(START + timedelta(minutes=12)) == pytest.approx(10.0)
    assert clock.real_seconds_until(START) == 0.0


def test_scaled_clock_monotonic_is_scaled_too():
    mono = FakeMonotonic()
    clock = ScaledClock(10, START, monotonic=mono, zone_resolver=utc_zone)
    first = clock.monotonic()
    mono.value += 3
    assert clock.monotonic() - first == pytest.approx(30.0)


@pytest.mark.parametrize("scale", [0, -1, float("inf"), float("nan")])
def test_scaled_clock_rejects_bad_scales(scale):
    with pytest.raises(ValueError, match="positive"):
        ScaledClock(scale, START, zone_resolver=utc_zone)


def test_scaled_clock_requires_aware_start():
    with pytest.raises(ValueError, match="aware"):
        ScaledClock(2, datetime(2026, 1, 1), zone_resolver=utc_zone)


# --- ManualClock ----------------------------------------------------------------------


def test_manual_clock_advance_moves_wall_and_monotonic_together():
    clock = ManualClock(START, tz=BERLIN)
    mono = clock.monotonic()
    clock.advance(timedelta(minutes=5))
    assert clock.now() == START + timedelta(minutes=5)
    assert clock.monotonic() - mono == 300
    assert clock.real_seconds_until(START + timedelta(minutes=6)) == 60
    assert clock.real_seconds_until(START) == 0


def test_manual_clock_jump_changes_only_the_wall_clock():
    clock = ManualClock(START)
    mono = clock.monotonic()
    clock.jump(START + timedelta(hours=8))
    assert clock.now() == START + timedelta(hours=8)
    assert clock.monotonic() == mono


def test_manual_clock_rejects_negative_advance_and_naive_start():
    clock = ManualClock(START)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="aware"):
        ManualClock(datetime(2026, 1, 1))


def test_manual_clock_timezone_change_applies_on_refresh():
    clock = ManualClock(START)
    assert clock.local_tz() == ZoneInfo("UTC")
    assert clock.refresh_local_tz() is False
    clock.set_system_tz(BERLIN)
    assert clock.local_tz() == ZoneInfo("UTC")
    assert clock.refresh_local_tz() is True
    assert clock.local_tz() == BERLIN
    clock.set_system_tz(BERLIN)
    assert clock.refresh_local_tz() is False


# --- resolve_local_zone ---------------------------------------------------------------


def test_tz_environment_variable_wins(tmp_path):
    assert resolve_local_zone({"TZ": ":Asia/Kolkata"}, tmp_path / "none", tmp_path / "none") == (
        ZoneInfo("Asia/Kolkata")
    )


def test_localtime_symlink_is_resolved(tmp_path):
    zoneinfo_dir = tmp_path / "usr/share/zoneinfo/Europe"
    zoneinfo_dir.mkdir(parents=True)
    target = zoneinfo_dir / "Berlin"
    target.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(target)
    assert resolve_local_zone({}, link, tmp_path / "none") == BERLIN


def test_etc_timezone_file_is_used(tmp_path):
    tz_file = tmp_path / "timezone"
    tz_file.write_text("America/New_York\n")
    assert resolve_local_zone({}, tmp_path / "none", tz_file) == ZoneInfo("America/New_York")


def test_invalid_names_fall_through_to_raw_localtime_data(tmp_path):
    raw = tmp_path / "localtime"
    raw.write_bytes(SYSTEM_BERLIN.read_bytes())
    zone = resolve_local_zone({"TZ": "Not/AZone"}, raw, tmp_path / "none")
    assert zone.key.startswith("localtime:")
    assert datetime(2026, 6, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=2)


def test_change_between_two_unnamed_zone_files_is_detected(tmp_path, monkeypatch):
    raw = tmp_path / "localtime"
    raw.write_bytes(SYSTEM_BERLIN.read_bytes())
    monkeypatch.setattr(clock_module.time, "tzset", lambda: None)
    clock = SystemClock(zone_resolver=lambda: resolve_local_zone({}, raw, tmp_path / "none"))
    assert clock.refresh_local_tz() is False
    raw.write_bytes(Path("/usr/share/zoneinfo/Asia/Kolkata").read_bytes())
    assert clock.refresh_local_tz() is True
    assert datetime(2026, 6, 1, tzinfo=clock.local_tz()).utcoffset() == timedelta(hours=5.5)


def test_unreadable_everything_falls_back_to_utc(tmp_path):
    assert resolve_local_zone({}, tmp_path / "none", tmp_path / "none") == ZoneInfo("UTC")


# --- clock_from_env -------------------------------------------------------------------


def test_no_variables_means_system_clock():
    assert isinstance(clock_from_env({}), SystemClock)


def test_time_scale_and_fake_now_select_a_scaled_clock():
    clock = clock_from_env(
        {"DAYBOOK_TIME_SCALE": "60", "DAYBOOK_FAKE_NOW": "2026-11-03T08:55:00+01:00"}
    )
    assert isinstance(clock, ScaledClock)
    assert clock.scale == 60
    assert abs(clock.now() - datetime(2026, 11, 3, 7, 55, tzinfo=UTC)) < timedelta(minutes=1)


def test_fake_now_without_offset_is_local_time(monkeypatch):
    monkeypatch.setattr(clock_module, "_system_zone", lambda: BERLIN)
    clock = clock_from_env({"DAYBOOK_FAKE_NOW": "2026-11-03T09:00"})
    assert isinstance(clock, ScaledClock)
    assert clock.scale == 1.0
    assert abs(clock.now() - datetime(2026, 11, 3, 8, 0, tzinfo=UTC)) < timedelta(seconds=5)


def test_scale_alone_starts_from_the_real_time():
    clock = clock_from_env({"DAYBOOK_TIME_SCALE": "2"})
    assert isinstance(clock, ScaledClock)
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=5)


@pytest.mark.parametrize(
    "environ",
    [
        {"DAYBOOK_TIME_SCALE": "fast"},
        {"DAYBOOK_TIME_SCALE": "-3"},
        {"DAYBOOK_FAKE_NOW": "yesterday"},
        {"DAYBOOK_TIME_SCALE": "1", "DAYBOOK_FAKE_NOW": ""},
    ],
)
def test_invalid_values_are_ignored(environ, caplog):
    assert isinstance(clock_from_env(environ), SystemClock)
