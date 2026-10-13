"""Live-agent simulation (app/sim/world.py): each drone's loop, heartbeats,
lossy radio, injected failures and re-planning -- and the safety rules
that must hold throughout."""
import pathlib

import pytest

from app.cbba.demo import sample_scenario
from app.cbba.models import CommsSpec
from app.safety.geofence import point_in_polygon
from app.sim.models import SIM_LIVE_LABEL, FailureEvent, SimConfig
from app.sim.world import run_simulation

REACHABLE = 9  # of 11 in the sample: P10 needs lidar (nobody has it), P11 is outside the fence


def lost(at, who):
    return FailureEvent(at_s=at, kind="drone_lost", drone_id=who)


def kinds(r, kind):
    return [e for e in r.events if e.kind == kind]


def assert_always_inside_fence(r, sc):
    fence = [p.model_dump() for p in sc.geofence.points]
    for f in r.frames:
        for d in f.drones:
            assert point_in_polygon(d.lat, d.lon, fence), (f.t, d.id)


def assert_only_safe_plans_flown(r):
    for ep in r.epochs:
        if ep.result.dispatch_allowed:
            assert ep.result.safety.valid


def test_nominal_mission_completes_with_one_planning_round():
    sc = sample_scenario()
    r = run_simulation(sc)
    s = r.summary
    assert r.label == SIM_LIVE_LABEL
    assert s["inspected_count"] == REACHABLE and s["double_inspected"] == []
    assert s["planning_rounds"] == 1 and s["false_alarms"] == 0 and s["messages_lost"] == 0
    assert set(s["not_inspected"]) == {"P10-mast", "P11-off-site"}
    assert all(d["status"] == "home" for d in s["drones"].values())
    reserve = sc.constraints.battery_reserve_pct
    assert all(d["battery"] >= reserve for d in s["drones"].values())
    assert_always_inside_fence(r, sc)
    assert len(r.frames) == int(s["duration_s"]) + 1


def test_same_seed_same_story():
    cfg = SimConfig(message_loss=0.3, message_delay_ticks=1, seed=7)
    a = run_simulation(sample_scenario(), cfg, [lost(40, "sim-2")])
    b = run_simulation(sample_scenario(), cfg, [lost(40, "sim-2")])
    assert a.model_dump() == b.model_dump()


def test_lost_drone_is_noticed_by_heartbeat_timeout_and_its_places_reassigned():
    r = run_simulation(sample_scenario(), SimConfig(heartbeat_timeout_s=5), [lost(40, "sim-2")])
    det = [e for e in kinds(r, "detect") if "sim-2" in e.text and "declares" in e.text]
    assert det and det[0].t == pytest.approx(45.0, abs=1.0)  # 5 s without news
    assert r.summary["planning_rounds"] == 2
    assert r.epochs[1].participants == ["sim-1", "sim-3"]
    assert r.summary["inspected_count"] == REACHABLE and r.summary["double_inspected"] == []
    assert r.summary["false_alarms"] == 0
    assert r.summary["drones"]["sim-2"]["status"] == "lost"
    assert_only_safe_plans_flown(r)
    assert_always_inside_fence(r, sample_scenario())


def test_broken_radio_is_a_false_alarm_and_the_cut_off_drone_follows_lost_link():
    r = run_simulation(sample_scenario(), SimConfig(), [FailureEvent(at_s=30, kind="radio_cut", drone_id="sim-3")])
    assert r.summary["false_alarms"] >= 1
    assert any(e.drone == "sim-3" for e in kinds(r, "lost-link"))
    # Honest outcome: sim-3 finished its own places while the others re-planned them.
    assert r.summary["double_inspected"]
    assert r.summary["inspected_count"] == REACHABLE
    assert all(d["status"] == "home" for d in r.summary["drones"].values())
    assert_always_inside_fence(r, sample_scenario())


def test_radio_restored_ends_lost_link():
    ev = [FailureEvent(at_s=30, kind="radio_cut", drone_id="sim-3"), FailureEvent(at_s=40, kind="radio_restored", drone_id="sim-3")]
    r = run_simulation(sample_scenario(), SimConfig(), ev)
    assert any("ends the lost-link procedure" in e.text for e in r.events)
    assert all(d["status"] == "home" for d in r.summary["drones"].values())


def test_sudden_battery_drop_sends_that_drone_home_and_others_take_over():
    r = run_simulation(sample_scenario(), SimConfig(), [FailureEvent(at_s=20, kind="battery_drop", drone_id="sim-1", battery_pct=21)])
    react = kinds(r, "react")
    assert react and react[0].drone == "sim-1" and "heads home" in react[0].text
    assert "sim-1" not in r.epochs[1].participants
    assert r.summary["inspected_count"] == REACHABLE
    assert r.summary["drones"]["sim-1"]["status"] == "home"


def test_lossy_delayed_radio_still_completes_safely():
    cfg = SimConfig(message_loss=0.3, message_delay_ticks=1, seed=3)
    r = run_simulation(sample_scenario(), cfg, [lost(40, "sim-2")])
    s = r.summary
    assert s["messages_lost"] > 0
    assert s["inspected_count"] == REACHABLE
    assert all(d["status"] in ("home", "lost") for d in s["drones"].values())
    assert_only_safe_plans_flown(r)
    assert_always_inside_fence(r, sample_scenario())


def test_plan_that_cannot_be_agreed_in_time_is_blocked_and_nobody_takes_off():
    cfg = SimConfig(message_loss=0.9, max_planning_ticks=5, seed=1)
    r = run_simulation(sample_scenario(), cfg)
    assert kinds(r, "blocked")
    assert r.summary["inspected_count"] == 0
    assert not r.epochs[0].result.dispatch_allowed
    assert all(f.drones[i].status != "flying" for f in r.frames for i in range(3))


def test_altitude_bands_never_change_between_planning_rounds():
    r = run_simulation(sample_scenario(), SimConfig(), [lost(40, "sim-2")])
    alt = {p.drone_id: p.altitude_m for p in r.epochs[0].result.plans}
    for ep in r.epochs[1:]:
        for p in ep.result.plans:
            assert p.altitude_m == alt[p.drone_id]


def test_offline_drone_and_range_limited_radio():
    sc = sample_scenario("full", offline=["sim-2"]).model_copy(update={"comms": CommsSpec(range_m=5000)})
    r = run_simulation(sc)
    assert r.summary["drones"]["sim-2"]["status"] == "offline"
    assert r.summary["inspected_count"] == REACHABLE


def test_sim_package_imports_no_llm_network_or_flight_library():
    import ast

    import app.sim.world as w

    forbidden = {"anthropic", "crewai", "groq", "openai", "litellm", "pymavlink", "requests", "httpx", "urllib", "socket"}
    for f in pathlib.Path(w.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for n in names:
                assert n.split(".")[0] not in forbidden, f"{f.name} imports {n}"


def _random_case(seed):
    import random

    from tests.cbba.helpers import random_scenario

    rng = random.Random(seed)
    sc = random_scenario(rng, connected_only=rng.random() < 0.5)
    ids = [d.drone_id for d in sc.drones]
    events = []
    for _ in range(rng.randint(0, 3)):
        kind = rng.choice(["drone_lost", "radio_cut", "radio_restored", "battery_drop"])
        events.append(FailureEvent(at_s=rng.uniform(5, 120), kind=kind, drone_id=rng.choice(ids), battery_pct=rng.uniform(15, 40) if kind == "battery_drop" else None))
    cfg = SimConfig(message_loss=rng.choice([0, 0, 0.1, 0.3]), message_delay_ticks=rng.choice([0, 0, 1, 2]), seed=seed)
    return sc, cfg, events


@pytest.mark.parametrize("seed", range(40))
def test_random_missions_keep_the_safety_rules(seed):
    sc, cfg, events = _random_case(seed)
    r = run_simulation(sc, cfg, events)
    assert_always_inside_fence(r, sc)
    assert_only_safe_plans_flown(r)
    # The run ends with nobody left mid-air (unless the time limit hit).
    if r.summary["duration_s"] < cfg.max_time_s:
        assert all(d["status"] in ("home", "lost", "offline") for d in r.summary["drones"].values()), r.summary["drones"]
    # Nothing is inspected that isn't a real place inside the fence.
    inside = {p.point_id for p in sc.points} - {x for x in r.summary["not_inspected"] if r.summary["not_inspected"][x] == "outside the geofence"}
    assert set(r.summary["inspected"]) <= inside


def test_last_drone_standing_keeps_working():
    """sim-2 crashes at 40 s, sim-3 at 70 s: sim-1 is alone but its radio is
    fine -- it must re-plan the leftovers, not give up as 'lost-link'."""
    r = run_simulation(sample_scenario(), SimConfig(), [lost(40, "sim-2"), lost(70, "sim-3")])
    assert any("only drone left in contact" in e.text for e in r.events)
    assert not any(e.kind == "lost-link" for e in r.events)
    assert r.summary["planning_rounds"] == 3
    assert r.summary["inspected_count"] == REACHABLE
    assert r.summary["drones"]["sim-1"]["status"] == "home"
