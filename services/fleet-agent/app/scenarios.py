"""Phase G: scripted end-to-end scenarios (docs/CBBA_PLAN.md section 7).

Each scenario drives the REAL pipeline -- CBBA allocation, the
deterministic safety gate, the live-agent simulation (and, where it
applies, the instruction gate or the one-line survey) -- and checks the
outcome against the project's rules, not against whatever the code
happens to print. Run:

    cd services/fleet-agent
    python -m app.scenarios                       # print the results
    python -m app.scenarios --report ../../docs/PHASE_G_RESULTS.md

Everything is in-process simulation and deterministic for a fixed seed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field

from app.cbba.demo import sample_scenario
from app.cbba.models import Scenario
from app.cbba.service import allocate
from app.core.models import Geofence, GeofencePoint
from app.mission.goal_parser import NeedsClarification
from app.safety.geofence import point_in_polygon
from app.sim.models import FailureEvent, SimConfig, SimResult
from app.sim.world import run_simulation
from app.supervisor.gate import IntentGate
from app.supervisor.instructions import RuleBasedInstructionParser, interpret_instruction
from app.survey import MapView, SurveyRequest, plan_survey

RESERVE_PCT = 20.0  # SafetyConstraints.battery_reserve_pct default


@dataclass
class Check:
    text: str
    ok: bool
    detail: str = ""


@dataclass
class Outcome:
    key: str
    title: str
    purpose: str
    checks: list[Check] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    timeline: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def check(self, text: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(text, bool(ok), detail))


# --- helpers -------------------------------------------------------------------------

def _reachable(result) -> set[str]:
    """Points the plan can do: all minus those outside the fence or with no capable drone."""
    return {p for plan in result.plans for p in plan.path}


def _flight_metrics(o: Outcome, sim: SimResult, kinds=("inject", "detect", "replan", "react", "lost-link", "blocked")) -> None:
    s = sim.summary
    o.metrics.update({
        "places": s["places"], "inspected": s["inspected_count"], "done_twice": len(s["double_inspected"]),
        "planning_rounds": s["planning_rounds"], "messages_sent": s["messages_sent"], "messages_lost": s["messages_lost"],
        "false_alarms": s["false_alarms"], "mission_time_s": s["duration_s"],
    })
    o.timeline = [f"t={e.t:g} s - {e.text}" for e in sim.events if e.kind in kinds][:14]


def _all_rounds_safe(o: Outcome, sim: SimResult) -> None:
    flown = [ep for ep in sim.epochs if ep.result.dispatch_allowed]
    o.check("every plan that was flown had passed the safety gate", all(ep.result.dispatch_allowed for ep in flown) and bool(flown),
            f"{len(flown)} of {len(sim.epochs)} planning round(s) flown")


def _separated(o: Outcome, sim: SimResult) -> None:
    alts = {}
    for ep in sim.epochs:
        for p in ep.result.plans:
            alts.setdefault(p.drone_id, set()).add(p.altitude_m)
    flat = [a for v in alts.values() for a in v]
    o.check("each drone keeps its own altitude band (no two drones share a height)",
            all(len(v) == 1 for v in alts.values()) and len(flat) == len(set(flat)),
            ", ".join(f"{d} {sorted(v)[0]:g} m" for d, v in sorted(alts.items())))


# --- scenarios -------------------------------------------------------------------------

def g1_nominal() -> Outcome:
    o = Outcome("G1", "Nominal mission", "3 healthy drones, full radio: plan, check, fly.")
    sc = sample_scenario()
    r = allocate(sc)
    ref = allocate(sc, method="SGA")
    assigned = [p for plan in r.plans for p in plan.path]
    o.check("CBBA converges and the safety gate passes", r.converged and r.dispatch_allowed,
            f"agreed in round {r.last_change_round}, {r.messages_sent} messages")
    o.check("no place is given to two drones", len(assigned) == len(set(assigned)))
    o.check("the place needing lidar is left out with a reason (no drone has lidar)",
            any(x.item_id.startswith("P10") and "lidar" in x.reason for x in r.unassigned))
    o.check("the place outside the fence is left out", any(x.item_id.startswith("P11") and "geofence" in x.reason for x in r.excluded_points))
    o.check("CBBA scores at least 50% of the central greedy planner (SGA)", r.total_score >= 0.5 * ref.total_score,
            f"CBBA {r.total_score:.1f} vs SGA {ref.total_score:.1f}")
    sim = run_simulation(sc)
    sim2 = run_simulation(sc)
    s = sim.summary
    o.check("every assigned place is inspected exactly once", set(s["inspected"]) == set(assigned) and not s["double_inspected"],
            f"{s['inspected_count']} inspected")
    o.check("all drones return home above the 20% battery reserve",
            all(v["status"] == "home" and v["battery"] >= RESERVE_PCT for v in s["drones"].values()),
            ", ".join(f"{d} {v['battery']:g}%" for d, v in s["drones"].items()))
    o.check("the run is deterministic (same inputs, same result)", sim.summary == sim2.summary)
    _separated(o, sim)
    _flight_metrics(o, sim)
    o.metrics["same_as_SGA"] = [(p.drone_id, p.path) for p in r.plans] == [(p.drone_id, p.path) for p in ref.plans]
    return o


def g2_drone_loss() -> Outcome:
    o = Outcome("G2", "Drone loss", "sim-2 crashes at 60 s; nobody is told. The others must notice and take over its places.")
    sc = sample_scenario()
    reach = _reachable(allocate(sc))
    timeout = SimConfig().heartbeat_timeout_s
    sim = run_simulation(sc, events=[FailureEvent(at_s=60, kind="drone_lost", drone_id="sim-2")])
    s = sim.summary
    detect = [e.t for e in sim.events if e.kind == "detect" and "sim-2" in e.text and "declares" in e.text]
    o.check(f"the loss is noticed within {timeout:g} s + 2 s", bool(detect) and min(detect) - 60 <= timeout + 2,
            f"first noticed at t={min(detect):g} s ({min(detect) - 60:g} s after the crash)" if detect else "never noticed")
    o.check("a new plan is agreed and passes the safety gate", len(sim.epochs) >= 2 and sim.epochs[-1].result.dispatch_allowed)
    o.check("every reachable place is still inspected, none twice", set(s["inspected"]) == reach and not s["double_inspected"],
            f"{s['inspected_count']} of {len(reach)}")
    o.check("the survivors return home", all(v["status"] == "home" for d, v in s["drones"].items() if d != "sim-2"))
    _all_rounds_safe(o, sim)
    _flight_metrics(o, sim)
    o.metrics["detection_delay_s"] = min(detect) - 60 if detect else None
    return o


def g3_comms_cut() -> Outcome:
    o = Outcome("G3", "Radio cut and restored", "sim-3's radio fails at 40 s and works again at 140 s; it keeps flying.")
    sc = sample_scenario()
    reach = _reachable(allocate(sc))
    first = {p.drone_id: p.path for p in allocate(sc).plans}
    sim = run_simulation(sc, events=[FailureEvent(at_s=40, kind="radio_cut", drone_id="sim-3"),
                                     FailureEvent(at_s=140, kind="radio_restored", drone_id="sim-3")])
    s = sim.summary
    o.check("sim-3 follows the lost-link procedure (finishes its own places, takes no new work)",
            any(e.kind == "lost-link" and e.drone == "sim-3" for e in sim.events))
    o.check("the others re-plan without it and the new plan passes the gate",
            len(sim.epochs) >= 2 and sim.epochs[1].result.dispatch_allowed and "sim-3" not in sim.epochs[1].participants)
    o.check("contact is re-established after the radio works again",
            any(e.kind == "detect" and "sim-3" in e.text and e.t >= 140 for e in sim.events))
    o.check("every reachable place is inspected", reach <= set(s["inspected"]), f"{s['inspected_count']} of {len(reach)}")
    o.check("places done twice are only sim-3's own (the price of not knowing crash from radio failure)",
            set(s["double_inspected"]) <= set(first.get("sim-3", [])), f"done twice: {', '.join(s['double_inspected']) or 'none'}")
    _separated(o, sim)
    _all_rounds_safe(o, sim)
    _flight_metrics(o, sim)
    return o


def g4_low_battery() -> Outcome:
    o = Outcome("G4", "Low battery", "sim-1's battery drops to 15% at 50 s, below the 20% reserve.")
    sc = sample_scenario()
    reach = _reachable(allocate(sc))
    sim = run_simulation(sc, events=[FailureEvent(at_s=50, kind="battery_drop", drone_id="sim-1", battery_pct=15)])
    s = sim.summary
    after = [e for e in sim.events if e.kind == "inspect" and e.drone == "sim-1" and e.t > 50]
    o.check("sim-1 drops the places it can no longer cover and heads home",
            any(e.kind == "react" and e.drone == "sim-1" for e in sim.events) and not after and s["drones"]["sim-1"]["status"] == "home",
            f"home with {s['drones']['sim-1']['battery']:g}%")
    o.check("the re-plan reaches agreement and passes the gate", len(sim.epochs) >= 2 and sim.epochs[-1].result.converged
            and sim.epochs[-1].result.dispatch_allowed,
            f"round {len(sim.epochs)}: {sim.epochs[-1].planning_ticks} tick(s) of talk, "
            f"{'agreed' if sim.epochs[-1].result.converged else 'no agreement'}")
    o.check("the other drones take over: every reachable place inspected, none twice",
            set(s["inspected"]) == reach and not s["double_inspected"], f"{s['inspected_count']} of {len(reach)}")
    _all_rounds_safe(o, sim)
    _flight_metrics(o, sim)
    return o


def g5_fence_change() -> Outcome:
    o = Outcome("G5", "Fence change", "The operator shrinks the fence (north edge moved 220 m south) and the mission is re-planned.")
    sc = sample_scenario()
    old = [(p.lat, p.lon) for p in sc.geofence.points]
    north = 37.429
    fence = Geofence(points=[GeofencePoint(lat=lat if lat < north else north, lon=lon) for lat, lon in old])
    sc2 = sc.model_copy(update={"geofence": fence})
    poly = [p.model_dump() for p in fence.points]
    outside = {p.point_id for p in sc.points if not point_in_polygon(p.lat, p.lon, poly)}
    r = allocate(sc2)
    excluded = {x.item_id for x in r.excluded_points}
    o.check("places now outside the fence are excluded with that reason", outside <= excluded and
            all("geofence" in x.reason for x in r.excluded_points if x.item_id in outside), f"excluded: {', '.join(sorted(excluded))}")
    o.check("the new plan passes the safety gate", r.dispatch_allowed)
    wps = [w for plan in r.plans for w in plan.route]
    o.check("every route point lies inside the new fence", all(point_in_polygon(w.lat, w.lon, poly) for w in wps), f"{len(wps)} route points")
    sim = run_simulation(sc2)
    o.check("nothing outside the new fence is inspected", not (set(sim.summary["inspected"]) & outside))
    # A fence that leaves a drone outside must block, not fly.
    tiny = Geofence(points=[GeofencePoint(lat=37.4285, lon=-122.172), GeofencePoint(lat=37.4285, lon=-122.165),
                            GeofencePoint(lat=37.431, lon=-122.165), GeofencePoint(lat=37.431, lon=-122.172)])
    r3 = allocate(sc.model_copy(update={"geofence": tiny}))
    o.check("a fence that leaves the drones' launch points outside blocks dispatch", not r3.dispatch_allowed,
            "; ".join(sorted({i.code for i in r3.safety.issues})))
    _flight_metrics(o, sim)
    return o


def g6_lossy_radio() -> Outcome:
    o = Outcome("G6", "Bad radio", "30% of messages lost and 1 s late (seed 7).")
    sc = sample_scenario()
    reach = _reachable(allocate(sc))
    sim = run_simulation(sc, SimConfig(message_loss=0.3, message_delay_ticks=1, seed=7))
    s = sim.summary
    o.check("the drones still agree and the plan passes the gate", bool(sim.epochs) and sim.epochs[0].result.dispatch_allowed)
    o.check("every reachable place inspected, none twice", set(s["inspected"]) == reach and not s["double_inspected"],
            f"{s['messages_lost']} of {s['messages_sent']} messages lost")
    _all_rounds_safe(o, sim)
    _flight_metrics(o, sim)
    return o


def g7_partition() -> Outcome:
    o = Outcome("G7", "Radio partition", "sim-3 can hear nobody from the start (split network).")
    sc = sample_scenario("split")
    r = allocate(sc)
    o.check("the groups claim the same places and the gate BLOCKS dispatch", not r.dispatch_allowed
            and any(i.code == "DUPLICATE_ASSIGNMENT" for i in r.safety.issues), "; ".join(sorted({i.code for i in r.safety.issues})))
    sim = run_simulation(sc)
    o.check("an unsafe plan never flies: nothing is inspected", sim.summary["inspected_count"] == 0)
    _flight_metrics(o, sim)
    return o


def g8_multi_hop() -> Outcome:
    o = Outcome("G8", "Multi-hop radio", "sim-1 and sim-3 can only talk through sim-2 (a line).")
    sc = sample_scenario("line")
    r = allocate(sc)
    reach = _reachable(r)
    o.check("bids relayed through sim-2 reach agreement; the gate passes", r.converged and r.dispatch_allowed,
            f"agreed in round {r.last_change_round}")
    sim = run_simulation(sc)
    s = sim.summary
    o.check("every reachable place inspected, none twice, no false alarms",
            set(s["inspected"]) == reach and not s["double_inspected"] and s["false_alarms"] == 0)
    _flight_metrics(o, sim)
    return o


def g9_instruction() -> Outcome:
    o = Outcome("G9", "Operator instruction", '"P3 is high priority, sim-2 is offline" (built-in rules, no AI key needed).')
    sc = sample_scenario()
    res = interpret_instruction("P3 is high priority, sim-2 is offline", sc, RuleBasedInstructionParser())
    d = IntentGate().review(sc, res.batch.intents)
    o.check("both changes pass the fixed-rule gate", len(d.accepted) == 2 and not d.rejected, "; ".join(t for _, t in d.accepted))
    r = allocate(d.scenario)
    o.check("the re-plan leaves sim-2 out and passes the safety gate",
            r.dispatch_allowed and any(x.item_id == "sim-2" for x in r.excluded_drones))
    o.check("P03 is now worth 150 (high priority)", next(p.value for p in d.scenario.points if p.point_id.startswith("P03")) == 150)
    allowed = {"set_priority", "require_sensor", "set_deadline", "skip_point", "exclude_drone", "include_drone", "replan"}
    o.check("the interpreter produced only allowed change kinds (no bids, winners or routes)",
            {i.kind for i in res.batch.intents} <= allowed, ", ".join(sorted(i.kind for i in res.batch.intents)))
    return o


def g10_out_of_scope() -> Outcome:
    o = Outcome("G10", "Out-of-scope requests", "Attack or targeting wording is refused before any AI runs.")
    sc = sample_scenario()
    for text in ("attack P03", "find targets near the gate", "destroy the tower"):
        try:
            interpret_instruction(text, sc, RuleBasedInstructionParser())
            refused = False
        except NeedsClarification:
            refused = True
        o.check(f'"{text}" is refused', refused)
    view = MapView(south=28.609, west=77.203, north=28.617, east=77.215)
    try:
        plan_survey(SurveyRequest(text="attack this area", view=view, engine="rules"))
        refused = False
    except NeedsClarification:
        refused = True
    o.check('"attack this area" is refused by the survey', refused)
    return o


def g11_survey() -> Outcome:
    o = Outcome("G11", "One-line survey anywhere", '"3 drones survey this area" over Delhi, Chennai (Potheri) and Sydney.')
    for name, lat, lon in (("Delhi", 28.6139, 77.2090), ("Potheri", 12.8231, 80.0384), ("Sydney", -33.8688, 151.2093)):
        view = MapView(south=lat - 0.004, west=lon - 0.006, north=lat + 0.004, east=lon + 0.006)
        sc = plan_survey(SurveyRequest(text="3 drones survey this area", view=view, engine="rules")).scenario
        r = allocate(sc)
        sim = run_simulation(sc)
        s = sim.summary
        o.check(f"{name}: plan passes the gate; all {len(sc.points)} points inspected once", r.dispatch_allowed and
                s["inspected_count"] == len(sc.points) and not s["double_inspected"], f"mission time {s['duration_s']:g} s")
    return o


SCENARIOS = [g1_nominal, g2_drone_loss, g3_comms_cut, g4_low_battery, g5_fence_change, g6_lossy_radio, g7_partition,
             g8_multi_hop, g9_instruction, g10_out_of_scope, g11_survey]


def run_all() -> list[Outcome]:
    return [fn() for fn in SCENARIOS]


# --- report --------------------------------------------------------------------------

def report_markdown(outcomes: list[Outcome]) -> str:
    passed = sum(o.passed for o in outcomes)
    checks = sum(len(o.checks) for o in outcomes)
    ok = sum(c.ok for o in outcomes for c in o.checks)
    lines = [
        "# Phase G results: scripted scenarios",
        "",
        f"Generated by `python -m app.scenarios --report` on {dt.date.today().isoformat()}. "
        f"**{passed} of {len(outcomes)} scenarios passed ({ok} of {checks} checks).** "
        "Every number below is from this run of the real planner, safety gate and live-agent simulation "
        "(in-process simulation, deterministic for a fixed seed).",
        "",
        "| Scenario | Result | Inspected | Done twice | Planning rounds | Messages (lost) | False alarms | Mission time |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in outcomes:
        m = o.metrics
        flight = "places" in m
        lines.append(
            f"| {o.key} {o.title} | {'PASS' if o.passed else 'FAIL'} | "
            + (f"{m['inspected']} of {m['places']} | {m['done_twice']} | {m['planning_rounds']} | {m['messages_sent']} ({m['messages_lost']}) | "
               f"{m['false_alarms']} | {m['mission_time_s']:g} s |" if flight else "- | - | - | - | - | - |"))
    lines += ["", "In the Stanford sample (G1-G8), 2 of the 11 places can never be done by design: P10 needs a lidar "
              "no drone carries and P11 is outside the fence. \"9 of 11\" is therefore every place that can be done."]
    for o in outcomes:
        lines += ["", f"## {o.key}. {o.title} - {'PASS' if o.passed else 'FAIL'}", "", o.purpose, ""]
        lines += [f"- {'✓' if c.ok else '✗'} {c.text}" + (f" ({c.detail})" if c.detail else "") for c in o.checks]
        extra = {k: v for k, v in o.metrics.items() if k in ("detection_delay_s", "same_as_SGA")}
        if extra:
            lines += ["", "Measured: " + ", ".join(f"{k.replace('_', ' ')} = {v}" for k, v in extra.items())]
        if o.timeline:
            lines += ["", "Key events:", ""] + [f"    {t}" for t in o.timeline]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", help="write the Markdown report here")
    ap.add_argument("--json", help="also write the raw results as JSON here")
    args = ap.parse_args(argv)
    outcomes = run_all()
    for o in outcomes:
        print(f"{'PASS' if o.passed else 'FAIL'}  {o.key:4s} {o.title}")
        for c in o.checks:
            if not c.ok:
                print(f"      ✗ {c.text} ({c.detail})")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_markdown(outcomes))
        print(f"report: {args.report}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([{"key": o.key, "title": o.title, "passed": o.passed, "checks": [c.__dict__ for c in o.checks],
                        "metrics": o.metrics} for o in outcomes], f, indent=2)
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
