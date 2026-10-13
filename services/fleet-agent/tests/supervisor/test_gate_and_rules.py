import pytest

from app.cbba.demo import sample_scenario
from app.cbba.models import DroneHealth
from app.cbba.service import allocate
from app.mission.goal_parser import NeedsClarification
from app.supervisor.gate import IntentGate
from app.supervisor.instructions import RuleBasedInstructionParser, interpret_instruction
from app.supervisor.intents import ExcludeDrone, IncludeDrone, RequestReplan, RequireSensor, SetDeadline, SetPriority, SkipPoint

RULES = RuleBasedInstructionParser()


def kinds(text, sc=None):
    r = interpret_instruction(text, sc or sample_scenario(), RULES)
    return [(i.kind, getattr(i, "point_id", None) or getattr(i, "drone_id", None), _val(i)) for i in r.batch.intents]


def _val(i):
    return getattr(i, "priority", None) or getattr(i, "sensor", None) or getattr(i, "deadline_s", None)


# --- rule-based parser -------------------------------------------------------------

def test_several_changes_in_one_sentence():
    assert kinds("P03 and P05 are high priority, P02 needs thermal, sim-2 is offline") == [
        ("set_priority", "P03-tower", "high"),
        ("set_priority", "P05-fence-east", "high"),
        ("require_sensor", "P02-gate", "thermal"),
        ("exclude_drone", "sim-2", None),
    ]


def test_short_names_minutes_any_drone_skip_back_replan():
    assert kinds("P01 within 2 minutes") == [("set_deadline", "P01-roof-north", 120.0)]
    assert kinds("p06 low priority then P09 any drone") == [
        ("set_priority", "P06-pipeline-a", "low"),
        ("require_sensor", "P09-transformer", "any"),
    ]
    assert kinds("skip P10 and replan") == [("skip_point", "P10-mast", None), ("replan", None, None)]
    assert kinds("sim-2 is back online") == [("include_drone", "sim-2", None)]
    assert kinds("P07 no deadline") == [("set_deadline", "P07-pipeline-b", None)]


def test_p1_is_not_confused_with_p10():
    sc = sample_scenario()
    sc = sc.model_copy(update={"points": [p.model_copy(update={"point_id": pid}) for p, pid in zip(sc.points[:2], ["P1", "P10"])]})
    assert kinds("P10 high priority", sc) == [("set_priority", "P10", "high")]


def test_unclear_text_gets_a_question():
    for text in ["hello", "make it better", "P99 high priority", ""]:
        with pytest.raises(NeedsClarification):
            interpret_instruction(text, sample_scenario(), RULES)


@pytest.mark.parametrize("text", ["attack P03", "strike the tower", "target sim-1", "bomb the car park", "shoot P02", "hit the enemy", "engage the hostile drone"])
def test_out_of_scope_refused(text):
    with pytest.raises(NeedsClarification, match="outside what this system does"):
        interpret_instruction(text, sample_scenario(), RULES)


# --- gate -------------------------------------------------------------------------------

def test_gate_applies_to_a_copy_and_canonicalises_ids():
    sc = sample_scenario()
    d = IntentGate().review(sc, [SetPriority(point_id="p03-TOWER", priority="high"), ExcludeDrone(drone_id="SIM-2")])
    assert [t for _, t in d.accepted] == ["Make P03-tower high priority", "Take sim-2 out of the mission (offline)"]
    tower = next(p for p in d.scenario.points if p.point_id == "P03-tower")
    assert tower.value == 150
    assert next(x for x in d.scenario.drones if x.drone_id == "sim-2").health == DroneHealth.OFFLINE
    # original untouched
    assert next(p for p in sc.points if p.point_id == "P03-tower").value == 150  # it was already 150 in the sample
    assert all(x.health == DroneHealth.OK for x in sc.drones)


def test_gate_rejects_unknown_ids_and_bad_values():
    d = IntentGate().review(sample_scenario(), [SetPriority(point_id="P99", priority="high"), SetDeadline(point_id="P01", deadline_s=99999), ExcludeDrone(drone_id="sim-9")])
    assert d.accepted == []
    reasons = [r for _, r in d.rejected]
    assert "no place called 'P99'" in reasons[0]
    assert "no place called 'P01'" in reasons[1]  # exact or case-insensitive full id only at the gate
    assert "no drone called 'sim-9'" in reasons[2]
    d = IntentGate().review(sample_scenario(), [SetDeadline(point_id="P01-roof-north", deadline_s=99999)])
    assert "between 1 s and 7200 s" in d.rejected[0][1]


def test_gate_keeps_at_least_one_drone_and_one_place():
    sc = sample_scenario()
    d = IntentGate().review(sc, [ExcludeDrone(drone_id=x.drone_id) for x in sc.drones])
    assert len(d.accepted) == 2 and "at least one drone" in d.rejected[0][1]
    d = IntentGate(max_per_batch=20).review(sc, [SkipPoint(point_id=p.point_id) for p in sc.points])
    assert len(d.accepted) == len(sc.points) - 1 and "at least one place" in d.rejected[0][1]


def test_gate_limits_batch_size_and_rate():
    sc = sample_scenario()
    d = IntentGate(max_per_batch=2).review(sc, [RequestReplan()] * 3)
    assert len(d.accepted) == 2 and "more than 2 changes" in d.rejected[0][1]
    t = [0.0]
    g = IntentGate(max_replans_per_minute=2, clock=lambda: t[0])
    assert g.review(sc, [RequestReplan()]).accepted
    assert g.review(sc, [RequestReplan()]).accepted
    blocked = g.review(sc, [RequestReplan()])
    assert not blocked.accepted and "too many change requests" in blocked.rejected[0][1]
    t[0] = 61.0
    assert g.review(sc, [RequestReplan()]).accepted


def test_include_drone_and_any_sensor():
    sc = sample_scenario("full", offline=["sim-2"])
    d = IntentGate().review(sc, [IncludeDrone(drone_id="sim-2"), RequireSensor(point_id="P10-mast", sensor="any")])
    assert next(x for x in d.scenario.drones if x.drone_id == "sim-2").health == DroneHealth.OK
    assert next(p for p in d.scenario.points if p.point_id == "P10-mast").required_capability is None


def test_end_to_end_instruction_changes_the_plan_through_cbba_and_safety():
    """'P10 any drone' makes the lidar point doable; 'sim-2 is offline'
    removes sim-2. CBBA re-plans and the safety gate still decides."""
    sc = sample_scenario()
    before = allocate(sc)
    assert before.winners["P10-mast"] is None
    r = interpret_instruction("P10 any drone, sim-2 is offline", sc, RULES)
    d = IntentGate().review(sc, r.batch.intents)
    after = allocate(d.scenario)
    assert after.winners["P10-mast"] in ("sim-1", "sim-3")
    assert all(p.drone_id != "sim-2" for p in after.plans)
    assert after.dispatch_allowed
