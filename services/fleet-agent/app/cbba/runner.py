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
from app.cbba.models import CbbaConfig, DroneSpec, InspectionPoint
from app.cbba.network import CommsGraph, InMemoryTransport
from app.core.models import SafetyConstraints


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
) -> CbbaRun:
    agents = {
        d.drone_id: CbbaAgent(d, points, constraints, config, altitudes[d.drone_id])
        for d in sorted(drones, key=lambda d: d.drone_id)
    }
    transport = InMemoryTransport(graph)
    needed_stable = max(1, graph.diameter())
    stable = 0
    rounds = 0
    last_change = 0
    converged = False

    for round_no in range(1, config.max_rounds + 1):
        rounds = round_no
        before = {aid: a.state() for aid, a in agents.items()}

        for agent in agents.values():
            agent.build_bundle()
        for agent in agents.values():
            msg = agent.message(round_no)
            for n in graph.neighbours(agent.id):
                transport.send(msg, n)
        for agent in agents.values():
            agent.consensus(transport.receive(agent.id), round_no)

        changed = any(agents[aid].state() != before[aid] for aid in agents)
        if changed:
            last_change = round_no
        stable = 0 if changed else stable + 1
        if stable >= needed_stable and groups_agree(agents, graph):
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
