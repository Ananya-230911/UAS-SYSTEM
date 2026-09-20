"""Phase 6b: AI-assisted risk monitoring
(docs/adr/0013-risk-monitoring-and-remote-id.md).

"AI-assisted" here means a small set of hand-written heuristic rules
over recent telemetry, not a trained model or an LLM call -- see the ADR
for why that's the right scope for this project rather than an
under-tested black box. Unlike the Phase 6a geofence failsafe
(app/main.py's ingest_telemetry(), docs/adr/0012-geofencing-failsafe.md),
this is deliberately advisory-only: assess_risk() never issues a
command. It surfaces flags for a human operator to act on.

Pure function, no DB/session access, so it's directly unit-testable
against synthetic samples (see tests/test_risk_monitor.py) without a
running API.
"""
from dataclasses import dataclass, field
from typing import Protocol


class TelemetryPoint(Protocol):
    """The minimal shape assess_risk() needs from a sample -- satisfied
    by both models.TelemetrySample and the in-memory row ingest_telemetry()
    builds before it's committed."""

    timestamp: object
    armed: bool
    flight_mode: str
    battery_pct: float | None
    gps_fix_type: int | None
    satellites_visible: int | None
    relative_alt_m: float | None
    groundspeed_ms: float | None


# Thresholds are simulator-appropriate constants for demonstrating the
# mechanism, not values tuned against real flight data -- see the ADR's
# "known limitation" on this.
BATTERY_DRAIN_PCT_PER_MIN_THRESHOLD = 5.0
LOW_GPS_FIX_TYPE_THRESHOLD = 3
LOW_SATELLITES_THRESHOLD = 6
ALTITUDE_DROP_M_THRESHOLD = 5.0
ABNORMAL_SPEED_MS_THRESHOLD = 30.0

# A vehicle already flying RTL or LANDING is *supposed* to lose
# altitude -- excluding those modes from the altitude-drop check is
# what keeps a normal landing (or the Phase 6a failsafe's own auto-RTL)
# from being flagged as if something had gone wrong.
DESCENDING_MODES = {"RTL", "LANDING"}

RISK_WEIGHTS = {
    "RAPID_BATTERY_DRAIN": 40,
    "GPS_DEGRADED": 30,
    "UNEXPECTED_ALTITUDE_DROP": 50,
    "ABNORMAL_SPEED": 25,
}

# score 0 -> LOW, 1..HIGH_THRESHOLD-1 -> MEDIUM, >=HIGH_THRESHOLD -> HIGH.
HIGH_RISK_SCORE_THRESHOLD = 50


@dataclass
class RiskAssessment:
    level: str  # LOW / MEDIUM / HIGH
    score: int
    flags: list[str] = field(default_factory=list)


def assess_risk(samples: list) -> RiskAssessment:
    """samples: chronological (oldest first), at least the most recent
    handful of TelemetryPoint-shaped samples for one vehicle. Returns
    LOW/score 0/no flags for an empty list -- there's nothing to assess
    yet, which is not itself a risk."""
    if not samples:
        return RiskAssessment(level="LOW", score=0, flags=[])

    flags: list[str] = []
    latest = samples[-1]

    if len(samples) >= 2:
        first = samples[0]
        if first.battery_pct is not None and latest.battery_pct is not None:
            # .replace(tzinfo=None) on both sides: SQLite (this project's
            # default storage, docs/adr/0004-telemetry-storage.md) silently
            # drops tzinfo on round-trip, so a sample already stored has a
            # naive timestamp while a not-yet-committed one (built fresh
            # with datetime.now(timezone.utc)) is still aware -- mixing
            # the two in a straight subtraction raises "can't subtract
            # offset-naive and offset-aware datetimes". Stripping tzinfo
            # from both is safe here since every timestamp this module
            # ever sees originates from the same UTC clock either way.
            elapsed_min = (
                latest.timestamp.replace(tzinfo=None) - first.timestamp.replace(tzinfo=None)
            ).total_seconds() / 60.0
            if elapsed_min > 0:
                drain_rate = (first.battery_pct - latest.battery_pct) / elapsed_min
                if drain_rate > BATTERY_DRAIN_PCT_PER_MIN_THRESHOLD:
                    flags.append("RAPID_BATTERY_DRAIN")

    if (
        latest.gps_fix_type is not None and latest.gps_fix_type < LOW_GPS_FIX_TYPE_THRESHOLD
    ) or (
        latest.satellites_visible is not None
        and latest.satellites_visible < LOW_SATELLITES_THRESHOLD
    ):
        flags.append("GPS_DEGRADED")

    if len(samples) >= 2:
        prev = samples[-2]
        if (
            latest.armed
            and latest.flight_mode not in DESCENDING_MODES
            and prev.relative_alt_m is not None
            and latest.relative_alt_m is not None
            and (prev.relative_alt_m - latest.relative_alt_m) > ALTITUDE_DROP_M_THRESHOLD
        ):
            flags.append("UNEXPECTED_ALTITUDE_DROP")

    if latest.groundspeed_ms is not None and latest.groundspeed_ms > ABNORMAL_SPEED_MS_THRESHOLD:
        flags.append("ABNORMAL_SPEED")

    score = sum(RISK_WEIGHTS[f] for f in flags)
    if score == 0:
        level = "LOW"
    elif score < HIGH_RISK_SCORE_THRESHOLD:
        level = "MEDIUM"
    else:
        level = "HIGH"
    return RiskAssessment(level=level, score=score, flags=flags)
