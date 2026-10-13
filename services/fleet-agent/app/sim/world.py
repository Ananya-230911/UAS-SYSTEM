"""Live-agent mission simulation (docs/CBBA_PLAN.md section 2A, Phase E).

Every drone runs its own loop each tick (tick_s seconds):

    observe      its own position, battery and the messages that reached it
    communicate  a heartbeat to every drone it can hear (who it is, which
                 places it knows are done, which planning "epoch" it is
                 in, and when it last had news of every teammate -- so
                 "still alive" news is relayed across a multi-hop
                 network); while planning, also its CBBA bids
    decide       CBBA bundle building + consensus (app/cbba/agent.py);
                 notices teammates that have gone silent
    act          flies its agreed route, hovers to inspect, returns home
    react        teammate silent for heartbeat_timeout_s -> declared lost
                 -> starts a new planning epoch for the places not yet
                 done; battery too low to finish -> drops its places,
                 heads home, and starts a new epoch; hears nobody at all
                 -> "lost-link": finishes its own places, takes no new work

The radio (app/sim/transport.py) can lose and delay messages. Drones are
NOT told about failures -- they notice through missing heartbeats, which
can be wrong (a drone with a broken radio is still flying). Every plan,
first or re-plan, goes through the same deterministic safety gate as
everywhere else (app/cbba/service.py::build_result) before any drone
follows it; a blocked plan sends the drones home.

The simulator itself is an observer with ground truth: it moves drones,
applies injected failures, and decides when an epoch has converged
(everyone in contact agrees and nothing changed for a few ticks). That
global check is a simulation convenience, labelled as such -- this is
not real distributed consensus (rule 4). No MAVLink, no vehicles.
"""
from __future__ import annotations

import random

from app.cbba.agent import CbbaAgent
from app.cbba.models import CommsSpec, DroneHealth, DroneSpec, InspectionPoint, Scenario
from app.cbba.network import CbbaMessage, CommsGraph
from app.cbba.scoring import endurance_issues, route_waypoints
from app.cbba.service import build_result, split_inputs
from app.core.models import AgentMessage, Waypoint
from app.safety.geofence import horizontal_distance_m
from app.safety.validator import assign_altitude_bands
from app.sim.models import (
    SIM_LIVE_LABEL,
    DroneFrame,
    EpochRecord,
    FailureEvent,
    Frame,
    SimConfig,
    SimEvent,
    SimResult,
)
from app.sim.transport import LossyTransport


def _dist(a_lat, a_lon, b_lat, b_lon) -> float:
    return horizontal_distance_m({"lat": a_lat, "lon": a_lon}, {"lat": b_lat, "lon": b_lon})


class SimDrone:
    def __init__(self, spec: DroneSpec, altitude: float, offline: bool):
        self.spec = spec
        self.id = spec.drone_id
        self.lat, self.lon = spec.start.lat, spec.start.lon
        self.home = spec.home_point()
        self.battery = spec.battery_pct
        self.altitude = altitude
        # Ground truth
        self.alive = not offline
        self.radio = not offline
        self.airborne = False
        self.status = "offline" if offline else "planning"
        # Plan being flown: point ids, then None for "home"
        self.queue: list[str | None] = []
        self.hover_left = 0.0
        self.hovering_at: str | None = None
        # The drone's own view
        self.epoch = 0
        self.epoch_points: list[str] = []
        self.epoch_excluded: list[str] = []
        self.epoch_dispatched = False
        self.agent: CbbaAgent | None = None
        # Gossip failure detector: for each teammate, the newest "I'm alive"
        # time stamp known (from it directly or relayed), and when (locally)
        # that last got newer. Silence is measured by the second, so relay
        # delay across several hops never looks like a failure.
        self.news_ts: dict[str, float] = {}
        self.progress_at: dict[str, float] = {}
        self.believed_lost: set[str] = set()
        self.known_inspected: set[str] = set()
        self.lost_link = False
        self.leaving = False  # dropped its places (low battery) -- takes no new work

    def spec_now(self) -> DroneSpec:
        return DroneSpec(
            drone_id=self.id,
            start=Waypoint(lat=self.lat, lon=self.lon, alt_m=0),
            home=Waypoint(lat=self.home.lat, lon=self.home.lon, alt_m=0),
            battery_pct=max(0.0, self.battery),
            capabilities=self.spec.capabilities,
        )


class MissionSim:
    def __init__(self, scenario: Scenario, config: SimConfig, events: list[FailureEvent] | None = None):
        self.sc = scenario
        self.cfg = config
        self.rng = random.Random(config.seed)
        self.transport = LossyTransport(config.message_loss, config.message_delay_ticks, self.rng)
        drones, excluded_drones, points, self.excluded_points = split_inputs(scenario)
        self.points: dict[str, InspectionPoint] = {p.point_id: p for p in points}
        taking_part = {d.drone_id for d in drones}
        # Altitudes are fixed for the whole mission, so a re-plan can never
        # put two drones at the same height (even one that lost its radio).
        self.altitudes = assign_altitude_bands(sorted(taking_part), scenario.constraints, scenario.base_altitude_m)
        self.drones: dict[str, SimDrone] = {}
        for d in sorted(scenario.drones, key=lambda d: d.drone_id):
            alt = self.altitudes.get(d.drone_id, scenario.base_altitude_m)
            self.drones[d.drone_id] = SimDrone(d, alt, offline=d.drone_id not in taking_part)
        known = set(self.drones)
        self.pending = sorted([e for e in (events or []) if e.drone_id in known], key=lambda e: e.at_s)
        self.t = 0.0
        self.tick = 0
        self.inspected_by: dict[str, list[str]] = {}
        self.events: list[SimEvent] = []
        self.frames: list[Frame] = []
        self.epochs: list[EpochRecord] = []
        self.planning: dict | None = None  # the epoch being planned (observer's bookkeeping)
        self.false_alarms = 0
        for x in excluded_drones:
            self.log("inject", x.item_id, f"{x.item_id} takes no part: {x.reason}.")
        for x in self.excluded_points:
            self.log("plan", None, f"{x.item_id} is ignored: {x.reason}.")
        self.team = [d.id for d in self.drones.values() if d.alive]

    # ---- helpers -------------------------------------------------------------
    def log(self, kind: str, drone: str | None, text: str) -> None:
        self.events.append(SimEvent(t=round(self.t, 1), kind=kind, drone=drone, text=text))

    def _graph(self) -> CommsGraph:
        up = [d for d in self.drones.values() if d.alive and d.radio]
        ids = [d.id for d in up]
        links: set[frozenset[str]] = set()
        spec = self.sc.comms
        for i, a in enumerate(up):
            for b in up[i + 1 :]:
                if spec.links is not None:
                    ok = (a.id, b.id) in spec.links or (b.id, a.id) in spec.links
                elif spec.range_m is not None:
                    ok = _dist(a.lat, a.lon, b.lat, b.lon) <= spec.range_m
                else:
                    ok = True
                if ok:
                    links.add(frozenset((a.id, b.id)))
        return CommsGraph(ids, links)

    def _remaining_points(self, known_done: set[str]) -> list[str]:
        return sorted(p for p in self.points if p not in known_done)

    # ---- planning epochs ---------------------------------------------------------
    def _join_epoch(self, d: SimDrone, epoch: int, points: list[str], excluded: list[str], dispatched: bool) -> None:
        d.epoch, d.epoch_points, d.epoch_excluded, d.epoch_dispatched = epoch, list(points), list(excluded), dispatched
        d.agent = None
        if dispatched:
            # Rejoined after the others already agreed: its old places were
            # re-planned without it, so it must not fly them too.
            if d.airborne:
                d.queue = [None]
                d.status = "returning"
                self.log("replan", d.id, f"{d.id} is back in contact, but the others already re-planned without it, so it returns home.")
            return
        if d.id in excluded and not d.leaving:
            # The team thinks this drone is lost and is re-planning without
            # it (a false alarm). Its places will be reassigned, so it goes
            # home rather than doing them twice.
            if d.airborne:
                d.queue = [None]
                d.hovering_at = None
                d.status = "returning"
                self.log("replan", d.id, f"{d.id} learns the team is re-planning without it, so it returns home.")
            return
        if d.leaving:
            return
        todo = [self.points[p] for p in points if p not in d.known_inspected]
        d.lost_link = False
        if not todo:
            # Nothing left that this drone knows of: it takes no part.
            self._head_home(d, "nothing left to inspect")
            return
        d.agent = CbbaAgent(d.spec_now(), todo, self.sc.constraints, self.sc.config, d.altitude)
        d.status = "holding" if d.airborne else "planning"
        d.queue = [] if d.airborne else d.queue

    def _start_epoch(self, initiator: SimDrone | None, reason: str, extra_excluded: list[str]) -> None:
        new = max(d.epoch for d in self.drones.values()) + 1
        if self.planning is not None:
            self.log("replan", None, f"Planning round {self.planning['epoch']} is replaced by round {new} before it finished.")
        known = initiator.known_inspected if initiator else set()
        excluded = sorted(set(extra_excluded) | (initiator.believed_lost if initiator else set()))
        points = self._remaining_points(known)
        self.planning = {"epoch": new, "started_t": self.t, "reason": reason, "initiator": initiator.id if initiator else None, "stable": 0, "ticks": 0, "last_state": None}
        if initiator is None:
            for d in self.drones.values():
                if d.alive:
                    self._join_epoch(d, new, points, excluded, False)
        else:
            self._join_epoch(initiator, new, points, excluded, False)
        self.log("replan" if initiator else "plan", initiator.id if initiator else None,
                 f"Planning round {new} starts ({reason}). {len(points)} place(s) to share." if initiator is None
                 else f"{initiator.id} starts re-planning round {new} ({reason}); the others join as the news reaches them.")

    def _members(self, epoch: int) -> list[SimDrone]:
        return [d for d in self.drones.values() if d.alive and d.radio and d.epoch == epoch and not d.epoch_dispatched and d.agent is not None]

    def _check_epoch(self, graph: CommsGraph) -> None:
        pl = self.planning
        if pl is None:
            return
        pl["ticks"] += 1
        e = pl["epoch"]
        members = self._members(e)
        # Everyone still in contact with the members must have joined.
        member_ids = {d.id for d in members}
        reachable = set()
        for comp in graph.components():
            if member_ids & set(comp):
                reachable |= set(comp)
        stragglers = [
            x for x in reachable - member_ids
            if self.drones[x].epoch < e and not self.drones[x].leaving and self.drones[x].id not in self.drones[x].epoch_excluded
        ]
        state = tuple((d.id, d.agent.state()) for d in members)
        agree = all(members[0].agent.z == d.agent.z for d in members[1:]) if members else True
        if state == pl["last_state"] and agree and not stragglers:
            pl["stable"] += 1
        else:
            pl["stable"] = 0
        pl["last_state"] = state
        needed = max(2, graph.diameter() + self.cfg.message_delay_ticks + 1)
        timed_out = pl["ticks"] >= self.cfg.max_planning_ticks
        if (members and pl["stable"] >= needed) or timed_out or not members:
            self._dispatch(e, members, converged=bool(members) and not timed_out, agree=agree)

    def _dispatch(self, epoch: int, members: list[SimDrone], converged: bool, agree: bool) -> None:
        pl = self.planning
        self.planning = None
        for d in self.drones.values():
            if d.epoch == epoch:
                d.epoch_dispatched = True
        if not members:
            self.log("plan", None, f"Round {epoch}: no drone can take on the remaining places.")
            self._settle_ground(epoch)
            return
        # Members may disagree about what is already done (lost heartbeats):
        # plan over every place any member still considers open.
        open_ids = sorted({pid for d in members for pid in d.agent.points})
        points = [self.points[p] for p in open_ids]
        if not points:
            for d in members:
                self._head_home(d, "nothing left to inspect")
            self._settle_ground(epoch)
            return
        scenario = Scenario(
            drones=[d.spec_now() for d in members],
            points=points,
            geofence=self.sc.geofence,
            constraints=self.sc.constraints,
            comms=CommsSpec(),
            config=self.sc.config,
            base_altitude_m=self.sc.base_altitude_m,
        )
        drones, excl_d, pts, excl_p = split_inputs(scenario)
        paths = {d.id: (list(d.agent.path), list(d.agent.bundle)) for d in members if d.id in {x.drone_id for x in drones}}
        result = build_result(
            scenario, drones, excl_d, pts, excl_p, self.altitudes, paths,
            method="CBBA", label=SIM_LIVE_LABEL, converged=converged, agreement=agree,
            groups=[sorted(d.id for d in members)], rounds=pl["ticks"], last_change=pl["ticks"], messages=self.transport.sent,
        )
        self.epochs.append(EpochRecord(epoch=epoch, started_t=round(pl["started_t"], 1), reason=pl["reason"], initiator=pl["initiator"],
                                       participants=sorted(d.id for d in members), planning_ticks=pl["ticks"], result=result))
        who = "; ".join(f"{p.drone_id}: {' > '.join(p.path) if p.path else 'nothing'}" for p in result.plans)
        if result.dispatch_allowed:
            self.log("plan", None, f"Round {epoch} agreed after {pl['ticks']} tick(s). Safety gate PASSED. {who}")
            for p in result.plans:
                d = self.drones[p.drone_id]
                d.agent = None
                d.queue = list(p.path) + [None]
                if p.path or d.airborne:
                    d.airborne = True
                    d.status = "flying" if p.path else "returning"
                else:
                    d.status = "home"
                    d.queue = []
        else:
            reasons = "; ".join(sorted({i.code for i in result.safety.issues}))
            self.log("blocked", None, f"Round {epoch}: safety gate BLOCKED ({reasons}). All drones in this round go home.")
            for d in members:
                d.agent = None
                self._head_home(d, "plan blocked")
        self._settle_ground(epoch)

    def _settle_ground(self, epoch: int) -> None:
        """Drones of this round still on the ground with no route: they stay home."""
        for d in self.drones.values():
            if d.alive and d.epoch == epoch and not d.airborne and d.status == "planning":
                d.status = "home"
                d.queue = []

    def _head_home(self, d: SimDrone, why: str) -> None:
        if d.airborne:
            d.queue = [None]
            d.status = "returning"
        else:
            d.queue = []
            d.status = "home"

    # ---- one tick -------------------------------------------------------------
    def _apply_injected(self) -> None:
        while self.pending and self.pending[0].at_s <= self.t + 1e-9:
            e = self.pending.pop(0)
            d = self.drones[e.drone_id]
            if not d.alive:
                continue
            if e.kind == "drone_lost":
                d.alive, d.status, d.queue = False, "lost", []
                self.log("inject", d.id, f"[injected] {d.id} is lost (e.g. crash). Nobody is told; the others must notice.")
            elif e.kind == "radio_cut":
                d.radio = False
                self.log("inject", d.id, f"[injected] {d.id}'s radio stops working. It is still flying.")
            elif e.kind == "radio_restored":
                d.radio = True
                self.log("inject", d.id, f"[injected] {d.id}'s radio works again.")
            elif e.kind == "battery_drop" and e.battery_pct is not None:
                d.battery = e.battery_pct
                self.log("inject", d.id, f"[injected] {d.id}'s battery suddenly drops to {e.battery_pct:.0f}%.")

    def _communicate(self, graph: CommsGraph) -> None:
        for d in self.drones.values():
            if not (d.alive and d.radio):
                continue
            hb = {
                "epoch": d.epoch, "points": d.epoch_points, "excluded": d.epoch_excluded, "dispatched": d.epoch_dispatched,
                "inspected": sorted(d.known_inspected), "leaving": d.leaving,
                "seen": {**d.news_ts, d.id: self.t},
            }
            for n in graph.neighbours(d.id):
                self.transport.send(AgentMessage(message_id=f"{d.id}-hb-{self.tick}", sender_id=d.id, recipient_id=n,
                                                 message_type="heartbeat", payload=hb, sequence=self.tick), n, self.tick)
                if d.agent is not None and not d.epoch_dispatched:
                    m = d.agent.message(self.tick)
                    self.transport.send(AgentMessage(message_id=f"{d.id}-cbba-{self.tick}", sender_id=d.id, recipient_id=n,
                                                     message_type="cbba", payload={"epoch": d.epoch, **m.model_dump()}, sequence=self.tick), n, self.tick)

    def _receive(self) -> None:
        for d in self.drones.values():
            if not d.alive:
                continue
            msgs = self.transport.receive(d.id, self.tick, d.radio)
            cbba: list[CbbaMessage] = []
            for m in msgs:
                p = m.payload
                if m.message_type == "heartbeat":
                    if d.lost_link:
                        d.lost_link = False
                        if d.airborne:
                            d.status = "returning" if d.queue == [None] else "flying"
                        self.log("detect", d.id, f"{d.id} is back in contact (heard {m.sender_id}); it ends the lost-link procedure.")
                    d.known_inspected |= set(p["inspected"])
                    # Relayed liveness: the sender's news about everyone else.
                    for o, ts in sorted(p.get("seen", {}).items()):
                        if o == d.id or ts <= d.news_ts.get(o, -1.0):
                            continue
                        d.news_ts[o] = ts
                        d.progress_at[o] = self.t
                        if o in d.believed_lost:
                            d.believed_lost.discard(o)
                            if o == m.sender_id:
                                self.log("detect", d.id, f"{d.id} hears {o} again.")
                            else:
                                self.log("detect", d.id, f"{d.id} learns {o} is alive (news relayed by {m.sender_id}).")
                    if p["epoch"] > d.epoch:
                        self._join_epoch(d, p["epoch"], p["points"], p["excluded"], p["dispatched"])
                elif m.message_type == "cbba" and p["epoch"] == d.epoch and d.agent is not None and not d.epoch_dispatched:
                    cbba.append(CbbaMessage(**{k: v for k, v in p.items() if k != "epoch"}))
            if d.agent is not None and not d.epoch_dispatched:
                d.agent.consensus(cbba, self.tick)

    def _detect(self) -> None:
        for d in self.drones.values():
            if not d.alive:
                continue  # a drone with a broken radio still runs its loop: it just hears nobody
            newly = []
            for o in self.team:
                if o == d.id or o in d.believed_lost:
                    continue
                # Only a teammate it has had news of can be declared lost.
                if o in d.progress_at and self.t - d.progress_at[o] > self.cfg.heartbeat_timeout_s:
                    d.believed_lost.add(o)
                    newly.append(o)
            if not newly:
                continue
            truly = [o for o in newly if not self.drones[o].alive]
            self.false_alarms += len(newly) - len(truly)
            self.log("detect", d.id, f"{d.id} has had no news of {', '.join(newly)} for {self.cfg.heartbeat_timeout_s:g} s and declares "
                     f"{'it' if len(newly) == 1 else 'them'} lost" + ("" if len(truly) == len(newly) else " (in truth still flying - a radio problem)") + ".")
            in_contact = [o for o in self.team if o != d.id and o not in d.believed_lost]
            # Everyone going silent at once most likely means MY radio is
            # broken -> lost-link. Only the last remaining teammate going
            # silent (others were lost earlier) -> assume I'm the last drone
            # and keep working. From inside a drone these can't be told
            # apart; both choices are safe (fixed altitude bands).
            if not in_contact and len(newly) == 1 and len(self.team) > 2:
                if d.radio and (self.planning is None or self.planning["epoch"] <= d.epoch):
                    self.log("replan", d.id, f"{d.id} is the only drone left in contact: it re-plans the remaining places on its own.")
                    self._start_epoch(d, f"{newly[0]} lost", [])
                continue
            if not in_contact:
                if not d.airborne and d.status == "planning":
                    d.agent, d.status, d.queue = None, "home", []
                    self.log("lost-link", d.id, f"{d.id} can hear nobody before take-off, so it stays on the ground.")
                elif d.airborne and not d.lost_link:
                    d.lost_link = True
                    d.status = "lost-link"
                    self.log("lost-link", d.id, f"{d.id} can hear nobody: lost-link procedure - it finishes its own places and takes no new work.")
                continue
            if d.radio and (self.planning is None or self.planning["epoch"] <= d.epoch):
                self._start_epoch(d, f"{', '.join(newly)} lost", [])

    def _bundles(self) -> None:
        for d in self.drones.values():
            if d.alive and d.agent is not None and not d.epoch_dispatched:
                d.agent.build_bundle()

    def _move(self) -> None:
        speed = self.sc.constraints.cruise_speed_ms
        for d in self.drones.values():
            if not d.alive or d.status in ("planning", "holding", "home", "offline") or not d.queue:
                continue
            step = speed * self.cfg.tick_s
            if d.hovering_at:
                d.hover_left -= self.cfg.tick_s
                if d.hover_left > 0:
                    continue
                self._inspected(d, d.hovering_at)
                d.hovering_at = None
                d.queue.pop(0)
                step = 0  # hovering used this tick
            while step > 0 and d.queue:
                target = d.queue[0]
                tlat, tlon = (self.points[target].lat, self.points[target].lon) if target else (d.home.lat, d.home.lon)
                gap = _dist(d.lat, d.lon, tlat, tlon)
                if gap <= step:
                    d.lat, d.lon = tlat, tlon
                    step -= gap
                    if target is None:
                        d.queue.pop(0)
                        d.airborne = False
                        d.status = "home"
                        self.log("home", d.id, f"{d.id} is back home with {d.battery:.0f}% battery.")
                        break
                    hover = self.points[target].inspect_duration_s
                    if hover > 0:
                        d.hovering_at, d.hover_left = target, hover
                        break
                    self._inspected(d, target)
                    d.queue.pop(0)
                else:
                    f = step / gap
                    d.lat += (tlat - d.lat) * f
                    d.lon += (tlon - d.lon) * f
                    step = 0
            if d.queue == [None] and d.status == "flying":
                d.status = "returning"

    def _inspected(self, d: SimDrone, pid: str) -> None:
        self.inspected_by.setdefault(pid, []).append(d.id)
        d.known_inspected.add(pid)
        twice = len(self.inspected_by[pid]) > 1
        self.log("inspect", d.id, f"{d.id} inspected {pid}" + (f" (already done by {self.inspected_by[pid][0]} - double work)" if twice else "") + ".")

    def _battery(self) -> None:
        drain = self.sc.constraints.battery_drain_pct_per_min * self.cfg.tick_s / 60
        for d in self.drones.values():
            if not (d.alive and d.airborne):
                continue
            d.battery -= drain
            if d.battery <= 0:
                d.battery, d.alive, d.status = 0.0, False, "lost"
                self.log("inject", d.id, f"{d.id} ran out of battery.")
                continue
            todo = [q for q in d.queue if q is not None]
            if d.leaving or not todo or d.status not in ("flying", "lost-link"):
                continue
            route = route_waypoints([self.points[q] for q in todo], d.altitude)
            if endurance_issues(d.spec_now(), route, self.sc.constraints):
                d.leaving = True
                d.queue = [None]
                d.hovering_at = None
                d.status = "returning"
                self.log("react", d.id, f"{d.id}'s battery ({d.battery:.0f}%) can no longer cover {', '.join(todo)} and the trip home: "
                         "it drops them and heads home.")
                if d.radio and not d.lost_link:
                    self._start_epoch(d, f"{d.id} low battery", [d.id])

    def _frame(self, graph: CommsGraph) -> None:
        self.frames.append(Frame(
            t=round(self.t, 1),
            drones=[DroneFrame(id=d.id, lat=d.lat, lon=d.lon, battery=round(max(0.0, d.battery), 1), status=d.status, epoch=d.epoch)
                    for d in self.drones.values()],
            inspected=sorted(self.inspected_by),
            links=graph.links(),
        ))

    def _finished(self) -> bool:
        if self.planning is not None:
            return False
        return all(not d.alive or not d.airborne for d in self.drones.values()) and self.tick > 0

    # ---- run ---------------------------------------------------------------------
    def run(self) -> SimResult:
        if not self.points or not [d for d in self.drones.values() if d.alive]:
            self.log("plan", None, "Nothing to do: no places inside the fence, or no drone available.")
            g = self._graph()
            self._frame(g)
            return self._result()
        self._start_epoch(None, "mission start", [])
        g = self._graph()
        self._frame(g)
        while self.t < self.cfg.max_time_s:
            self.tick += 1
            self.t = self.tick * self.cfg.tick_s
            self._apply_injected()
            g = self._graph()
            self._bundles()
            self._communicate(g)
            self._receive()
            self._detect()
            self._check_epoch(g)
            self._move()
            self._battery()
            self._frame(g)
            if self._finished():
                break
        else:
            self.log("plan", None, f"Simulation stopped at the {self.cfg.max_time_s:g} s limit.")
        return self._result()

    def _result(self) -> SimResult:
        done = sorted(self.inspected_by)
        last = self.epochs[-1].result if self.epochs else None
        reasons = {x.item_id: x.reason for x in (last.unassigned if last else [])}
        not_done = {}
        for pid in sorted(self.points):
            if pid not in self.inspected_by:
                not_done[pid] = reasons.get(pid, "not reached (its drone was lost, or the mission ended first)")
        for x in self.excluded_points:
            not_done[x.item_id] = x.reason
        return SimResult(
            frames=self.frames,
            events=self.events,
            epochs=self.epochs,
            summary={
                "duration_s": round(self.t, 1),
                "inspected": done,
                "inspected_count": len(done),
                "places": len(self.points) + len(self.excluded_points),
                "double_inspected": sorted(p for p, ds in self.inspected_by.items() if len(ds) > 1),
                "not_inspected": not_done,
                "planning_rounds": len(self.epochs),
                "messages_sent": self.transport.sent,
                "messages_lost": self.transport.lost,
                "false_alarms": self.false_alarms,
                "drones": {d.id: {"status": d.status, "battery": round(max(0.0, d.battery), 1)} for d in self.drones.values()},
            },
        )


def run_simulation(scenario: Scenario, config: SimConfig | None = None, events: list[FailureEvent] | None = None) -> SimResult:
    return MissionSim(scenario, config or SimConfig(), events).run()
