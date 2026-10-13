"""One simulated drone's CBBA logic (Choi, Brunet & How 2009).

Each drone keeps its own view of the world:

    y[point]  highest bid it knows of for the point
    z[point]  which drone it thinks won the point (None = nobody)
    s[drone]  the round it last had news about that drone
    bundle    points it has won, in the order it won them
    path      the same points, in the order it will fly them

Each round it (1) builds its bundle -- greedily adds the point that
raises its path score the most, as long as that beats the best bid it
knows of; then (2) after hearing from neighbours, applies the paper's
Table I to settle each disagreement, and (3) drops any point it was
outbid on plus every point it added after that one (paper eq. 6),
because their bids were computed assuming the lost point was still in
the path.

Ties are always broken the same way: higher bid, then lower drone id,
then lower point id. Same inputs -> same answer.
"""
from __future__ import annotations

from app.cbba.models import CbbaConfig, DroneSpec, InspectionPoint
from app.cbba.network import CbbaMessage
from app.cbba.scoring import BID_EPS, best_insertion, evaluate_path, outbids
from app.core.models import SafetyConstraints

UPDATE, RESET, LEAVE = "update", "reset", "leave"


def decide_action(
    i: str,
    k: str,
    z_k: str | None,
    y_k: float,
    z_i: str | None,
    y_i: float,
    s_k: dict[str, float],
    s_i: dict[str, float],
) -> str:
    """Table I of the paper: receiver i, sender k, one point. z_k/y_k is
    what k believes about the point, z_i/y_i what i believes, s_k/s_i
    their timestamps. Returns UPDATE (take k's view), RESET (forget the
    point's winner) or LEAVE (keep i's view)."""

    def k_newer_about(m: str) -> bool:
        return s_k.get(m, -1.0) > s_i.get(m, -1.0)

    def i_newer_about(m: str) -> bool:
        return s_i.get(m, -1.0) > s_k.get(m, -1.0)

    if z_k == k:  # sender thinks it won
        if z_i == i:
            return UPDATE if outbids(y_k, k, y_i, i) else LEAVE
        if z_i == k or z_i is None:
            return UPDATE
        m = z_i
        return UPDATE if (k_newer_about(m) or outbids(y_k, k, y_i, m)) else LEAVE

    if z_k == i:  # sender thinks the receiver won
        if z_i == i or z_i is None:
            return LEAVE
        if z_i == k:
            return RESET
        return RESET if k_newer_about(z_i) else LEAVE

    if z_k is None:  # sender thinks nobody won
        if z_i == i or z_i is None:
            return LEAVE
        if z_i == k:
            return UPDATE
        return UPDATE if k_newer_about(z_i) else LEAVE

    m = z_k  # sender thinks a third drone m won
    if z_i == i:
        return UPDATE if (k_newer_about(m) and outbids(y_k, m, y_i, i)) else LEAVE
    if z_i == k:
        return UPDATE if k_newer_about(m) else RESET
    if z_i == m or z_i is None:
        return UPDATE if k_newer_about(m) else LEAVE
    n = z_i  # receiver thinks a fourth drone n won
    if k_newer_about(m) and k_newer_about(n):
        return UPDATE
    if k_newer_about(m) and outbids(y_k, m, y_i, n):
        return UPDATE
    if k_newer_about(n) and i_newer_about(m):
        return RESET
    return LEAVE


class CbbaAgent:
    def __init__(
        self,
        drone: DroneSpec,
        points: list[InspectionPoint],
        constraints: SafetyConstraints,
        config: CbbaConfig,
        altitude_m: float,
    ):
        self.drone = drone
        self.id = drone.drone_id
        self.points = {p.point_id: p for p in points}
        self.point_ids = sorted(self.points)
        self.constraints = constraints
        self.config = config
        self.altitude_m = altitude_m
        self.y: dict[str, float] = {pid: 0.0 for pid in self.point_ids}
        self.z: dict[str, str | None] = {pid: None for pid in self.point_ids}
        self.s: dict[str, float] = {self.id: 0.0}
        self.bundle: list[str] = []
        self.path: list[str] = []

    # --- bundle phase -------------------------------------------------

    def _path_points(self, path: list[str]) -> list[InspectionPoint]:
        return [self.points[pid] for pid in path]

    def path_score(self) -> float:
        ev = evaluate_path(self.drone, self._path_points(self.path), self.constraints, self.config, self.altitude_m)
        # A path built only by feasible insertions, then shortened by
        # removing points, stays feasible (triangle inequality), so None
        # here would be a bug -- fail loudly rather than guess.
        if ev is None:
            raise RuntimeError(f"{self.id}: own path became infeasible")
        return ev.score

    def warp_cap(self) -> float:
        """Bid warping (Johnson, Choi, Ponda & How, CDC 2012): a new bid
        may not exceed any bid already in this drone's bundle."""
        return min((self.y[pid] for pid in self.bundle), default=float("inf"))

    def build_bundle(self) -> None:
        """Add points one at a time. The drone picks the point with the
        highest TRUE marginal gain, but the bid it publishes is warped:
        capped at its previous bids (see warp_cap).

        Why: with travel time, two nearby points are cheaper together, so
        a point can be worth MORE after another point is added. That
        breaks the paper's "diminishing marginal gain" assumption and
        lets drones trade a point back and forth forever -- found by a
        randomised check during Phase B. Warped bids always shrink along
        a bundle, which restores the convergence guarantee."""
        while len(self.bundle) < self.config.max_bundle_size:
            current = self.path_score()
            path_pts = self._path_points(self.path)
            cap = self.warp_cap()
            best: tuple[float, float, str, int] | None = None  # (gain, bid, point, position)
            for pid in self.point_ids:  # sorted, so a tie goes to the lower id
                if pid in self.bundle:
                    continue
                ins = best_insertion(
                    self.drone, path_pts, current, self.points[pid], self.constraints, self.config, self.altitude_m
                )
                if ins is None:
                    continue
                gain, pos = ins
                bid = min(gain, cap)
                if bid <= BID_EPS or not outbids(bid, self.id, self.y[pid], self.z[pid]):
                    continue
                if best is None or gain > best[0] + BID_EPS:
                    best = (gain, bid, pid, pos)
            if best is None:
                return
            _, bid, pid, pos = best
            self.bundle.append(pid)
            self.path.insert(pos, pid)
            self.y[pid] = bid
            self.z[pid] = self.id

    # --- communication ------------------------------------------------

    def message(self, round_no: int) -> CbbaMessage:
        self.s[self.id] = float(round_no)
        return CbbaMessage(
            sender_id=self.id,
            round=round_no,
            winning_bids=dict(self.y),
            winners=dict(self.z),
            timestamps=dict(self.s),
        )

    # --- consensus phase ----------------------------------------------

    def consensus(self, messages: list[CbbaMessage], round_no: int) -> None:
        for msg in messages:  # already sorted by sender id
            k = msg.sender_id
            for pid in self.point_ids:
                action = decide_action(
                    self.id,
                    k,
                    msg.winners.get(pid),
                    msg.winning_bids.get(pid, 0.0),
                    self.z[pid],
                    self.y[pid],
                    msg.timestamps,
                    self.s,
                )
                if action == UPDATE:
                    self.y[pid] = msg.winning_bids.get(pid, 0.0)
                    self.z[pid] = msg.winners.get(pid)
                elif action == RESET:
                    self.y[pid] = 0.0
                    self.z[pid] = None
            # Paper eq. 5: news straight from a neighbour is as fresh as
            # this round; news it relays about others is as fresh as its own.
            for other, t in msg.timestamps.items():
                if other != self.id and t > self.s.get(other, -1.0):
                    self.s[other] = t
            self.s[k] = float(round_no)
        self._release_lost_points()

    def _release_lost_points(self) -> None:
        """Paper eq. 6: find the first bundle point now won by someone
        else; drop it and everything added after it. Later points' own
        claims are withdrawn (bid 0, no winner) so others may take them."""
        lost_at = next((n for n, pid in enumerate(self.bundle) if self.z[pid] != self.id), None)
        if lost_at is None:
            return
        for pid in self.bundle[lost_at + 1 :]:
            if self.z[pid] == self.id:
                self.y[pid] = 0.0
                self.z[pid] = None
        kept = set(self.bundle[:lost_at])
        self.bundle = self.bundle[:lost_at]
        self.path = [pid for pid in self.path if pid in kept]

    def state(self) -> tuple:
        """Everything that counts as 'my view changed' (timestamps don't)."""
        return (tuple(self.bundle), tuple(self.path), tuple(self.z.items()), tuple(self.y.items()))
