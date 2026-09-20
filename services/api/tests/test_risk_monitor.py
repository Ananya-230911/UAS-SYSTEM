from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.risk_monitor import assess_risk


@dataclass
class FakeSample:
    """Minimal stand-in for a TelemetrySample -- assess_risk() is a pure
    function over plain attributes, so tests don't need a DB row."""

    timestamp: datetime
    armed: bool = True
    flight_mode: str = "MISSION"
    battery_pct: float | None = 90.0
    gps_fix_type: int | None = 3
    satellites_visible: int | None = 10
    relative_alt_m: float | None = 20.0
    groundspeed_ms: float | None = 5.0


def t(seconds_from_start: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds_from_start)


def test_no_samples_is_low_risk():
    result = assess_risk([])
    assert result.level == "LOW"
    assert result.score == 0
    assert result.flags == []


def test_stable_telemetry_is_low_risk():
    samples = [FakeSample(timestamp=t(i * 10)) for i in range(5)]
    result = assess_risk(samples)
    assert result.level == "LOW"
    assert result.flags == []


def test_rapid_battery_drain_is_flagged():
    # 20% drop in 60 seconds = 20%/min, well over the 5%/min threshold.
    samples = [
        FakeSample(timestamp=t(0), battery_pct=90.0),
        FakeSample(timestamp=t(60), battery_pct=70.0),
    ]
    result = assess_risk(samples)
    assert "RAPID_BATTERY_DRAIN" in result.flags
    assert result.level in ("MEDIUM", "HIGH")


def test_battery_drain_handles_mixed_aware_and_naive_timestamps():
    # Regression test: SQLite (this project's default storage) silently
    # drops tzinfo on round-trip, so a sample already read back from the
    # DB has a naive timestamp while a freshly-built, not-yet-committed
    # one is still timezone-aware -- assess_risk() is called with
    # exactly this mix on every real telemetry ingest once there's at
    # least one prior stored sample. Reproduced live via
    # scripts/smoke_test.py, which (unlike the API's own unit tests) is
    # the first path that sends a real battery_pct across multiple
    # samples for one vehicle, immediately raising "can't subtract
    # offset-naive and offset-aware datetimes" before this fix.
    naive_first = FakeSample(timestamp=datetime(2026, 1, 1, 0, 0, 0), battery_pct=90.0)
    aware_latest = FakeSample(
        timestamp=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc), battery_pct=70.0
    )
    result = assess_risk([naive_first, aware_latest])  # must not raise
    assert "RAPID_BATTERY_DRAIN" in result.flags


def test_slow_battery_drain_is_not_flagged():
    # 2% drop over 60 seconds = 2%/min, under the threshold.
    samples = [
        FakeSample(timestamp=t(0), battery_pct=90.0),
        FakeSample(timestamp=t(60), battery_pct=88.0),
    ]
    result = assess_risk(samples)
    assert "RAPID_BATTERY_DRAIN" not in result.flags


def test_low_gps_fix_is_flagged():
    samples = [FakeSample(timestamp=t(0), gps_fix_type=1, satellites_visible=10)]
    result = assess_risk(samples)
    assert "GPS_DEGRADED" in result.flags


def test_low_satellite_count_is_flagged():
    samples = [FakeSample(timestamp=t(0), gps_fix_type=3, satellites_visible=3)]
    result = assess_risk(samples)
    assert "GPS_DEGRADED" in result.flags


def test_good_gps_is_not_flagged():
    samples = [FakeSample(timestamp=t(0), gps_fix_type=3, satellites_visible=12)]
    result = assess_risk(samples)
    assert "GPS_DEGRADED" not in result.flags


def test_sudden_altitude_drop_while_armed_is_flagged():
    samples = [
        FakeSample(timestamp=t(0), armed=True, flight_mode="MISSION", relative_alt_m=30.0),
        FakeSample(timestamp=t(1), armed=True, flight_mode="MISSION", relative_alt_m=10.0),
    ]
    result = assess_risk(samples)
    assert "UNEXPECTED_ALTITUDE_DROP" in result.flags


def test_altitude_drop_during_rtl_is_not_flagged():
    # Regression guard: RTL/LANDING are *supposed* to lose altitude --
    # including them here would make the Phase 6a failsafe's own auto-RTL
    # (or a completely normal landing) look like a risk signal.
    samples = [
        FakeSample(timestamp=t(0), armed=True, flight_mode="RTL", relative_alt_m=30.0),
        FakeSample(timestamp=t(1), armed=True, flight_mode="RTL", relative_alt_m=5.0),
    ]
    result = assess_risk(samples)
    assert "UNEXPECTED_ALTITUDE_DROP" not in result.flags


def test_altitude_drop_while_disarmed_is_not_flagged():
    samples = [
        FakeSample(timestamp=t(0), armed=False, flight_mode="STANDBY", relative_alt_m=30.0),
        FakeSample(timestamp=t(1), armed=False, flight_mode="STANDBY", relative_alt_m=0.0),
    ]
    result = assess_risk(samples)
    assert "UNEXPECTED_ALTITUDE_DROP" not in result.flags


def test_abnormal_speed_is_flagged():
    samples = [FakeSample(timestamp=t(0), groundspeed_ms=45.0)]
    result = assess_risk(samples)
    assert "ABNORMAL_SPEED" in result.flags


def test_multiple_flags_reach_high_risk():
    samples = [
        FakeSample(
            timestamp=t(0), battery_pct=90.0, gps_fix_type=3, relative_alt_m=30.0, groundspeed_ms=5.0
        ),
        FakeSample(
            timestamp=t(60),
            battery_pct=50.0,  # rapid drain
            gps_fix_type=0,  # GPS degraded
            relative_alt_m=30.0,
            groundspeed_ms=5.0,
        ),
    ]
    result = assess_risk(samples)
    assert "RAPID_BATTERY_DRAIN" in result.flags
    assert "GPS_DEGRADED" in result.flags
    assert result.level == "HIGH"


def test_never_raises_on_missing_fields():
    # None values throughout must be handled gracefully, not crash --
    # a vehicle that hasn't sent a GPS fix yet, for instance.
    samples = [
        FakeSample(
            timestamp=t(0),
            battery_pct=None,
            gps_fix_type=None,
            satellites_visible=None,
            relative_alt_m=None,
            groundspeed_ms=None,
        )
    ]
    result = assess_risk(samples)
    assert result.level == "LOW"
