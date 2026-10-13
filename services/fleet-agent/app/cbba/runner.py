"""Run synchronous CBBA rounds until the drones agree (or a hard limit).

One round, for every drone in the same order:
  1. bundle phase  -- add points it can win
  2. send its view (y, z, s) to every neighbour it can currently hear
  3. consensus     -- apply Table I to each message, drop lost points

Converged = no drone's view changed for D rounds in a row (D = the
comms graph's diameter, at least 1) AND every group of drones that can
hear each other holds identical winner lists. Not converging within
config.max_rounds is reported, and blocks dispatch.

This is a simulation: everything runs in one process, in lock-step
rounds (label "CBBA-sync-sim (in-process)").
"""
from __future__ import annotations

from dataclasses import dataclass

from app.cbba.agent import CbbaAgent
from app.cbba.models import CbbaConfig, DroneSpec, InspectionPoint, TraceEvent, TraceRound
from app.cbba.network import CommsGraph, InMemoryTransport
from app.core.models import SafetyConstraints


MAX_TRACE_ROUNDS = 60


def _trace_round(round_no, agents, before, wants, want_bids, graph, messages) -> TraceRound:
    events: list[TraceEvent] = []
    for aid, a in agents.items():
        had = set(before[aid][0])  # bundle at the start of the round
        for pid in wants[aid]:
            if pid not in had:
                events.append(TraceEvent(kind="bid", drone=aid, point=pid, bid=round(want_bids[aid][pid], 2)))
        lost_earlier = False
        for pid in wants[aid]:
            if pid in a.bundle:
                continue
            winner = a.z[pid]
            if winner is not None and winner != aid:
                events.append(
                    TraceEvent(
                        kind="dropped", drone=aid, point=pid, bid=round(want_bids[aid][pid], 2),
                        reason="outbid", winner=winner, winner_bid=round(a.y[pid], 2),
                    )
                )
                lost_earlier = True
            else:
                events.append(
                    TraceEvent(kind="dropped", drone=aid, point=pid, reason="after_lost" if lost_earlier else "reset")
                )
    return TraceRound(
        round=round_no,
        links=graph.links(),
        messages=messages,
        wants=wants,
        claims={aid: list(a.bundle) for aid, a in agents.items()},
        events=events,
    )


@dataclass
class CbbaRun:
    agents: dict[str, CbbaAgent]
    rounds: int
    last_change_round: int
    converged: bool
    agreement: bool
    messages_sent: int


def groups_agree(agents: dict[str, CbbaAgent], graph: CommsGraph) -> bool:
    for group in graph.components():
        first = agents[group[0]]
        for other in group[1:]:
            if agents[other].z != first.z or agents[other].y != first.y:
                return False
    return True


def run_cbba(
    drones: list[DroneSpec],
    points: list[InspectionPoint],
    constraints: SafetyConstraints,
    config: CbbaConfig,
    graph: CommsGraph,
    altitudes: dict[str, float],
    link_events: dict[int, list[tuple[str, str, bool]]] | None = None,
    trace: list[TraceRound] | None = None,
) -> CbbaRun:
    """link_events: optional {round: [(drone_a, drone_b, up), ...]} --
    links cut (up=False) or restored (up=True) at the start of that
    round, to simulate radios dropping out and coming back. The run is
    never declared converged before the last scheduled event.

    trace: optional list; one TraceRound per round (up to
    MAX_TRACE_ROUNDS) is appended, recording what each drone claimed and
    gave up, and why -- read-only, for display."""
    link_events = link_events or {}
    last_event = max(link_events, default=0)
    agents = {
        d.drone_id: CbbaAgent(d, points, constraints, config, altitudes[d.drone_id])
        for d in sorted(drones, key=lambda d: d.drone_id)
    }
    transport = InMemoryTransport(graph)
    stable = 0
    rounds = 0
    last_change = 0
    converged = False

    for round_no in range(1, config.max_rounds + 1):
        rounds = round_no
        for a, b, up in link_events.get(round_no, []):
            graph.set_link(a, b, up)
            stable = 0  # the network changed: start counting quiet rounds again
        before = {aid: a.state() for aid, a in agents.items()}

        sent_before = transport.sent
        for agent in agents.values():
            agent.build_bundle()
        wants = {aid: list(a.bundle) for aid, a in agents.items()}
        want_bids = {aid: {p: a.y[p] for p in a.bundle} for aid, a in agents.items()}
        for agent in agents.values():
            msg = agent.message(round_no)
            for n in graph.neighbours(agent.id):
                transport.send(msg, n)
        for agent in agents.values():
            agent.consensus(transport.receive(agent.id), round_no)

        if trace is not None and len(trace) < MAX_TRACE_ROUNDS:
            trace.append(_trace_round(round_no, agents, before, wants, want_bids, graph, transport.sent - sent_before))

        changed = any(agents[aid].state() != before[aid] for aid in agents)
        if changed:
            last_change = round_no
        stable = 0 if changed else stable + 1
        needed_stable = max(1, graph.diameter())
        if round_no >= last_event and stable >= needed_stable and groups_agree(agents, graph):
            converged = True
            break

    return CbbaRun(
        agents=agents,
        rounds=rounds,
        last_change_round=last_change,
        converged=converged,
        agreement=groups_agree(agents, graph),
        messages_sent=transport.sent,
    )
