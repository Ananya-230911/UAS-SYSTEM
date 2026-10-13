"""allocate() end to end: scenarios, randomised properties, the regression
case that cycled before bid warping, the demo, and the package boundary
(no LLM, no flight commands)."""
import ast
import pathlib
import random

import pytest

from app.cbba import agent as agent_module
from app.cbba.demo import main as demo_main
from app.cbba.demo import sample_scenario
from app.cbba.models import SIM_LABEL, CbbaConfig, CommsSpec, DroneHealth, Scenario
from app.cbba.network import CommsGraph
from app.cbba.service import SGA_LABEL, allocate
from tests.cbba.helpers import assignment, drone, point, random_scenario, scenario

DATA = pathlib.Path(__file__).parent / "data"


def owners(result) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in result.plans:
        for pid in p.path:
            out.setdefault(pid, []).append(p.drone_id)
    return out


# --- the sample scenario -------------------------------------------------------

def test_sample_full_comms_passes_and_matches_reference():
    sc = sample_scenario("full")
    r = allocate(sc)
    assert r.label == SIM_LABEL == "CBBA-sync-sim (in-process)"
    assert r.converged and r.agreement and r.safety.valid and r.dispatch_allowed
    assert assignment(r) == assignment(allocate(sc, method="SGA"))


def test_sample_points_that_cannot_be_done_say_why():
    r = allocate(sample_scenario("full"))
    assert {x.item_id: x.reason for x in r.unassigned} == {
        "P10-mast": "no available drone carries a 'lidar' sensor"
    }
    assert [x.item_id for x in r.excluded_points] == ["P11-off-site"]
    assert r.winners["P10-mast"] is None and "P11-off-site" not in r.winners


def test_thermal_points_only_go_to_thermal_drones():
    sc = sample_scenario("full")
    r = allocate(sc)
    caps = {d.drone_id: d.capabilities for d in sc.drones}
    for p in sc.points:
        if p.required_capability and r.winners.get(p.point_id):
            assert p.required_capability in caps[r.winners[p.point_id]]


def test_deadlines_are_met():
    r = allocate(sample_scenario("full"))
    plan = next(p for p in r.plans if "P09-transformer" in p.path)
    assert plan.arrival_s[plan.path.index("P09-transformer")] <= 120


def test_lost_drone_points_are_reallocated():
    """A drone going offline: re-running CBBA hands every point it had to
    the remaining drones (nothing is silently dropped)."""
    before = allocate(sample_scenario("full"))
    lost = next(p for p in before.plans if p.drone_id == "sim-2").path
    assert lost
    after = allocate(sample_scenario("full", offline=["sim-2"]))
    assert [x.item_id for x in after.excluded_drones] == ["sim-2"]
    assert all(p.drone_id != "sim-2" for p in after.plans)
    for pid in lost:
        assert after.winners[pid] in ("sim-1", "sim-3")
    assert after.dispatch_allowed


def test_relayed_comms_reach_the_same_answer_more_slowly():
    full, line = allocate(sample_scenario("full")), allocate(sample_scenario("line"))
    assert assignment(full) == assignment(line)
    assert line.last_change_round >= full.last_change_round
    assert line.converged and line.dispatch_allowed


def test_split_comms_is_blocked_not_guessed():
    r = allocate(sample_scenario("split"))
    assert r.converged and r.agreement  # each group agrees internally...
    assert not r.dispatch_allowed  # ...but the groups clash
    assert {i.code for i in r.safety.issues} == {"DUPLICATE_ASSIGNMENT"}
    assert any(len(ds) > 1 for ds in owners(r).values())


# --- edge cases ----------------------------------------------------------------

def test_drone_starting_outside_fence_takes_no_part():
    sc = scenario([drone("in"), drone("out", lat=37.4400)], [point("p", 37.4280, -122.1690)])
    r = allocate(sc)
    assert [(x.item_id, x.reason) for x in r.excluded_drones] == [("out", "starts outside the geofence")]
    assert r.winners == {"p": "in"} and r.dispatch_allowed


def test_no_available_drones_blocks():
    sc = scenario([drone("a", health=DroneHealth.OFFLINE)], [point("p", 37.4280, -122.1690)])
    r = allocate(sc)
    assert not r.dispatch_allowed
    assert "NO_AVAILABLE_DRONES" in {i.code for i in r.safety.issues}


def test_point_out_of_reach_says_so():
    sc = scenario([drone("a", 37.4252, -122.1718, battery=20.5)], [point("far", 37.4308, -122.1652)])
    r = allocate(sc)
    assert r.winners == {"far": None}
    assert r.unassigned[0].reason.startswith("out of reach")
    assert r.dispatch_allowed  # an empty plan is safe; nothing unsafe was assigned


def test_bundle_limit_is_respected():
    pts = [point(f"p{j}", 37.4260 + j * 0.0004, -122.1690) for j in range(6)]
    r = allocate(scenario([drone("a")], pts, config=CbbaConfig(max_bundle_size=2)))
    assert len(r.plans[0].path) == 2
    assert len(r.unassigned) == 4
    assert all("bundle limit" in x.reason for x in r.unassigned)


def test_hitting_the_round_limit_blocks_dispatch():
    sc = sample_scenario("line").model_copy(update={"config": CbbaConfig(max_rounds=1)})
    r = allocate(sc)
    assert not r.converged and not r.dispatch_allowed
    assert "NOT_CONVERGED" in {i.code for i in r.safety.issues}


def test_sga_result_is_labelled_as_a_reference():
    r = allocate(sample_scenario("full"), method="SGA")
    assert r.label == SGA_LABEL and r.messages_sent == 0
    with pytest.raises(ValueError):
        allocate(sample_scenario("full"), method="LLM")


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        scenario([drone("a"), drone("a")], [point("p", 37.428, -122.169)])
    with pytest.raises(ValueError):
        scenario([drone("a")], [point("p", 37.428, -122.169), point("p", 37.429, -122.169)])


# --- randomised properties -------------------------------------------------------

N_RANDOM = 120


def _random_cases():
    rng = random.Random(2026)
    return [random_scenario(rng, connected_only=(i % 2 == 0)) for i in range(N_RANDOM)]


CASES = _random_cases()


@pytest.mark.parametrize("sc", CASES, ids=[f"case{i}" for i in range(N_RANDOM)])
def test_random_scenario_properties(sc: Scenario):
    r = allocate(sc)
    # Converges, every connected group agrees, nothing given twice, safe.
    assert r.converged and r.agreement
    assert all(len(ds) == 1 for ds in owners(r).values())
    assert r.safety.valid and r.dispatch_allowed, r.safety.issues
    # Deterministic: same input, same output.
    assert r.model_dump() == allocate(sc).model_dump()
    # Settles within the paper's bound: N_min * D rounds,
    # N_min = min(points, drones * bundle limit).
    graph = CommsGraph.from_spec(sc.comms, sc.drones)
    n_min = min(len(sc.points), len(sc.drones) * sc.config.max_bundle_size)
    assert r.last_change_round <= n_min * max(1, graph.diameter())


@pytest.mark.parametrize("sc", CASES, ids=[f"case{i}" for i in range(N_RANDOM)])
def test_cbba_keeps_at_least_half_of_the_central_greedy_score(sc: Scenario):
    """The paper's guarantee: CBBA scores at least 50% of the optimum. SGA
    never beats the optimum, so CBBA >= 50% of SGA's score is a necessary
    consequence -- checked on every random case."""
    cbba, sga = allocate(sc), allocate(sc, method="SGA")
    assert cbba.total_score >= 0.5 * sga.total_score - 1e-9


def test_cbba_usually_matches_central_greedy_exactly():
    """Not a guarantee: drones bid simultaneously, so one can briefly
    believe a bid that its owner withdraws in the same round, fill its
    last bundle slot with something else, and never revisit it (random
    case 44 here; explained in ADR-0018). Measured on these fixed cases
    so a regression in consensus shows up as a drop."""
    same = sum(assignment(allocate(sc)) == assignment(allocate(sc, method="SGA")) for sc in CASES)
    assert same >= 0.95 * len(CASES), f"only {same}/{len(CASES)} identical to SGA"


# --- regression: the case that cycled forever before bid warping ----------------

def _cycling_case() -> Scenario:
    return Scenario.model_validate_json((DATA / "cycling_without_bid_warping.json").read_text())


def test_cycling_case_now_converges():
    r = allocate(_cycling_case())
    assert r.converged and r.dispatch_allowed
    assert all(len(ds) == 1 for ds in owners(r).values())


def test_cycling_case_really_cycles_without_bid_warping(monkeypatch):
    """Proves the regression case still exercises the bug: switch warping
    off and it must fail to converge (and the gate must block it)."""
    monkeypatch.setattr(agent_module.CbbaAgent, "warp_cap", lambda self: float("inf"))
    sc = _cycling_case().model_copy(update={"config": _cycling_case().config.model_copy(update={"max_rounds": 100})})
    r = allocate(sc)
    assert not r.converged and not r.dispatch_allowed


# --- demo & package boundary ----------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--comms", "line"], ["--comms", "split"], ["--offline", "sim-2"]])
def test_demo_runs(args, capsys):
    assert demo_main(args) == 0
    out = capsys.readouterr().out
    assert "CBBA-sync-sim (in-process)" in out and "never dispatches" in out
    if args == ["--comms", "split"]:
        assert "BLOCKED" in out and "not meaningful" in out


FORBIDDEN = {"anthropic", "crewai", "groq", "openai", "litellm", "pymavlink", "requests", "httpx", "urllib", "socket"}


def test_cbba_package_imports_no_llm_network_or_flight_library():
    """Rule 1 and rule 5 of docs/CBBA_PLAN.md, enforced: nothing in
    app/cbba can call an LLM, the network, or a vehicle."""
    pkg = pathlib.Path(agent_module.__file__).parent
    for f in pkg.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in FORBIDDEN, f"{f.name} imports {name}"


# --- comms cut, then restored (partition and merge) ------------------------------

def _run_with_link_events(events):
    from app.cbba.runner import run_cbba
    from app.cbba.service import _split_inputs
    from app.safety.validator import assign_altitude_bands

    sc = sample_scenario("full")
    drones, _, points, _ = _split_inputs(sc)
    alts = assign_altitude_bands([d.drone_id for d in drones], sc.constraints, sc.base_altitude_m)
    graph = CommsGraph.from_spec(sc.comms, drones)
    return run_cbba(drones, points, sc.constraints, sc.config, graph, alts, link_events=events), graph


CUT_SIM3 = [("sim-1", "sim-3", False), ("sim-2", "sim-3", False)]
RESTORE_SIM3 = [("sim-1", "sim-3", True), ("sim-2", "sim-3", True)]


def _claims(run):
    out: dict[str, list[str]] = {}
    for a in run.agents.values():
        for pid in a.bundle:
            out.setdefault(pid, []).append(a.id)
    return out


def test_partition_then_merge_reaches_one_conflict_free_answer():
    """sim-3 loses its radio from round 1 and gets it back in round 6.
    While cut off it claims points the others also hold; after the merge,
    consensus (timestamps decide whose news is fresher) removes every
    double claim and all three drones agree."""
    run, graph = _run_with_link_events({1: CUT_SIM3, 6: RESTORE_SIM3})
    assert run.converged and run.agreement
    assert graph.components() == [["sim-1", "sim-2", "sim-3"]]
    assert all(len(ds) == 1 for ds in _claims(run).values())
    assert run.rounds > 6


def test_partition_that_never_heals_leaves_double_claims_for_the_gate():
    run, graph = _run_with_link_events({1: CUT_SIM3})
    assert run.converged and run.agreement  # each group agrees internally
    assert graph.components() == [["sim-1", "sim-2"], ["sim-3"]]
    assert any(len(ds) > 1 for ds in _claims(run).values())


def test_run_is_never_declared_converged_before_a_scheduled_link_change():
    run, _ = _run_with_link_events({1: CUT_SIM3, 40: RESTORE_SIM3})
    assert run.rounds >= 40
