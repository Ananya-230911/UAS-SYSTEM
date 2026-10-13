"""Who can talk to whom, and how CBBA messages get there.

Version 1 is an in-memory queue inside one Python process (labelled
"CBBA-sync-sim (in-process)" everywhere). The Transport protocol is the
seam where a real inter-process transport could plug in later -- and
only then, once tested, may anything claim real distributed consensus
(docs/CBBA_PLAN.md rule 4).
"""
from __future__ import annotations

from collections import deque
from typing import Protocol

from pydantic import BaseModel

from app.cbba.models import CommsSpec, DroneSpec
from app.safety.geofence import horizontal_distance_m


class CbbaMessage(BaseModel):
    """One drone's view, sent to a neighbour after a round (paper's y, z, s).
    Travels as the payload of the existing app/core/models.py AgentMessage
    shape once a real bus exists; in-process it is passed directly."""

    sender_id: str
    round: int
    winning_bids: dict[str, float]  # y: point_id -> highest bid known
    winners: dict[str, str | None]  # z: point_id -> drone believed to win it
    timestamps: dict[str, float]  # s: drone_id -> when sender last had news of it


class Transport(Protocol):
    def send(self, msg: CbbaMessage, to: str) -> None: ...

    def receive(self, drone_id: str) -> list[CbbaMessage]: ...


class CommsGraph:
    """Undirected graph of working radio links between drones."""

    def __init__(self, drone_ids: list[str], links: set[frozenset[str]]):
        self.drone_ids = sorted(drone_ids)
        self._links = set(links)

    @classmethod
    def from_spec(cls, spec: CommsSpec, drones: list[DroneSpec]) -> CommsGraph:
        ids = [d.drone_id for d in drones]
        links: set[frozenset[str]] = set()
        if spec.links is not None:
            for a, b in spec.links:
                if a not in ids or b not in ids:
                    raise ValueError(f"link {a}-{b} names an unknown drone")
                if a != b:
                    links.add(frozenset((a, b)))
        else:
            for i, a in enumerate(drones):
                for b in drones[i + 1 :]:
                    in_range = spec.range_m is None or horizontal_distance_m(
                        {"lat": a.start.lat, "lon": a.start.lon},
                        {"lat": b.start.lat, "lon": b.start.lon},
                    ) <= spec.range_m
                    if in_range:
                        links.add(frozenset((a.drone_id, b.drone_id)))
        return cls(ids, links)

    def neighbours(self, drone_id: str) -> list[str]:
        return sorted(o for o in self.drone_ids if o != drone_id and frozenset((drone_id, o)) in self._links)

    def links(self) -> list[tuple[str, str]]:
        return sorted(tuple(sorted(link)) for link in self._links)

    def set_link(self, a: str, b: str, up: bool) -> None:
        """Cut or restore one link (used by failure scenarios)."""
        key = frozenset((a, b))
        if up:
            self._links.add(key)
        else:
            self._links.discard(key)

    def components(self) -> list[list[str]]:
        """Groups of drones that can reach each other, directly or by relay."""
        seen: set[str] = set()
        groups = []
        for start in self.drone_ids:
            if start in seen:
                continue
            group, queue = [], deque([start])
            seen.add(start)
            while queue:
                cur = queue.popleft()
                group.append(cur)
                for n in self.neighbours(cur):
                    if n not in seen:
                        seen.add(n)
                        queue.append(n)
            groups.append(sorted(group))
        return groups

    def diameter(self) -> int:
        """Longest shortest-hop path inside any connected group (D in the
        paper). 0 for a single drone or no links at all."""
        best = 0
        for start in self.drone_ids:
            dist = {start: 0}
            queue = deque([start])
            while queue:
                cur = queue.popleft()
                for n in self.neighbours(cur):
                    if n not in dist:
                        dist[n] = dist[cur] + 1
                        queue.append(n)
            best = max(best, max(dist.values()))
        return best


class InMemoryTransport:
    """A queue per drone. Delivery happens only over links that are up in
    the CommsGraph at send time; anything else is silently lost, as it
    would be over a broken radio link."""

    def __init__(self, graph: CommsGraph):
        self.graph = graph
        self._inbox: dict[str, list[CbbaMessage]] = {d: [] for d in graph.drone_ids}
        self.sent = 0
        self.dropped = 0

    def send(self, msg: CbbaMessage, to: str) -> None:
        if to in self.graph.neighbours(msg.sender_id):
            self._inbox[to].append(msg)
            self.sent += 1
        else:
            self.dropped += 1

    def receive(self, drone_id: str) -> list[CbbaMessage]:
        msgs = sorted(self._inbox[drone_id], key=lambda m: m.sender_id)
        self._inbox[drone_id] = []
        return msgs
