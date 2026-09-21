from app.decision import GUIDELINES, recommend_action


def test_routine_scenario_recommends_continue():
    rec = recommend_action(detections=[], obstacles=[], armed=True, battery_pct=90, gps_fix_type=3)
    assert rec.action == "CONTINUE"
    assert rec.matched_guideline["id"] == "G5"


def test_close_obstacle_recommends_divert():
    rec = recommend_action(
        detections=[],
        obstacles=[{"bearing_deg": 90, "range_m": 3.0}],
        armed=True,
        battery_pct=90,
        gps_fix_type=3,
    )
    assert rec.action == "DIVERT"
    assert rec.matched_guideline["id"] == "G1"


def test_person_in_path_recommends_hold():
    rec = recommend_action(
        detections=[{"class_name": "person", "confidence": 0.9, "bbox": {}}],
        obstacles=[],
        armed=True,
        battery_pct=90,
        gps_fix_type=3,
    )
    assert rec.action == "HOLD"


def test_person_never_results_in_continue_regardless_of_other_factors():
    # Black-box invariant guardrail 3 exists to guarantee, exercised
    # across a spread of battery/GPS combinations.
    for battery in (None, 10, 50, 90):
        for gps in (None, 0, 2, 3):
            rec = recommend_action(
                detections=[{"class_name": "person", "confidence": 0.9, "bbox": {}}],
                obstacles=[],
                armed=True,
                battery_pct=battery,
                gps_fix_type=gps,
            )
            assert rec.action != "CONTINUE"


def test_degraded_gps_plus_obstacle_recommends_rtl():
    rec = recommend_action(
        detections=[],
        obstacles=[{"bearing_deg": 45, "range_m": 4.0}],
        armed=True,
        battery_pct=90,
        gps_fix_type=1,
    )
    assert rec.action == "RTL"
    assert rec.matched_guideline["id"] == "G3"


def test_low_battery_plus_obstacle_recommends_rtl():
    rec = recommend_action(
        detections=[],
        obstacles=[{"bearing_deg": 45, "range_m": 4.0}],
        armed=True,
        battery_pct=15,
        gps_fix_type=3,
    )
    assert rec.action == "RTL"


def test_low_battery_never_results_in_divert_or_hold():
    # Black-box invariant the battery guardrail exists to guarantee: an
    # obstacle alone (no low-battery context in the query) retrieves G1
    # -> DIVERT; retrieval-driven guidelines that already fold battery
    # into their own text (G4) may reach RTL without the guardrail
    # needing to intervene -- either path is correct as long as the
    # *outcome* is never DIVERT/HOLD while critically low on battery.
    for obstacle_range in (2.0, 3.0, 5.9):
        rec = recommend_action(
            detections=[],
            obstacles=[{"bearing_deg": 45, "range_m": obstacle_range}],
            armed=True,
            battery_pct=10,
            gps_fix_type=3,
        )
        assert rec.action not in ("DIVERT", "HOLD")


def test_guardrail_disarmed_suppresses_in_flight_action():
    # An obstacle would normally recommend DIVERT, but a disarmed
    # vehicle has nothing to divert -- guardrail forces CONTINUE (no-op).
    rec = recommend_action(
        detections=[],
        obstacles=[{"bearing_deg": 45, "range_m": 3.0}],
        armed=False,
        battery_pct=90,
        gps_fix_type=3,
    )
    assert rec.action == "CONTINUE"
    armed_check = next(c for c in rec.guardrail_checks if "armed" in c["rule"])
    assert armed_check["passed"] is False


def test_every_recommendation_carries_all_three_guardrail_checks():
    rec = recommend_action(detections=[], obstacles=[], armed=True, battery_pct=90, gps_fix_type=3)
    assert len(rec.guardrail_checks) == 3


def test_recommendation_action_is_always_a_valid_command():
    valid_actions = {"CONTINUE", "HOLD", "DIVERT", "RTL"}
    for guideline in GUIDELINES:
        rec = recommend_action(detections=[], obstacles=[], armed=True, battery_pct=90, gps_fix_type=3)
        assert rec.action in valid_actions
