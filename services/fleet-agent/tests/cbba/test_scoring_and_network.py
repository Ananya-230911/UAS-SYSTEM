"""Path scoring, insertion, tie-breaking, the comms graph and the
in-memory transport."""
import math

import pytest

from app.cbba.models import CbbaConfig, CommsSpec
from app.cbba.network import CbbaMessage, CommsGraph, InMemoryTransport
from app.cbba.scoring import best_insertion, can_do, evaluate_path, outbids, route_waypoints
from app.core.models import SafetyConstraints
from app.safety.geofence import horizontal_distance_m
from app.safety.validator import check_endurance
from tests.cbba.helpers import drone, point

C = SafetyConstraints()
CFG = CbbaConfig()


# --- scoring -----------------------------------------------------------------

def test_score_is_value_times_discount_to_the_arrival_time():
    d = drone("a", 37.4270, -122.1690)
    p = point("p", 37.4280, -122.1690, value=100)
    dist = horizontal_distance_m({"lat": 37.4270, "lon": -122.1690}, {"lat": 37.4280, "lon": -122.1690})
    ev = evaluate_path(d, [p], C, CFG, 20.0)
    t = dist / C.cruise_speed_ms
    assert ev.arrival_s == [pytest.approx(t)]
    assert ev.score == pytest.approx(100 * CFG.discount_per_s ** t)
    assert ev.route_m == pytest.approx(2 * dist)


def test_hover_time_delays_the_next_point():
    d = drone("a", 37.4270, -122.1690)
    p1 = point("p1", 37.4280, -122.1690, inspect_duration_s=30)
    p2 = point("p2", 37.4290, -122.1690)
    ev = evaluate_path(d, [p1, p2], C, CFG, 20.0)
    leg = horizontal_distance_m({"lat": 37.4280, "lon": -122.1690}, {"lat": 37.4290, "lon": -122.1690})
    assert ev.arrival_s[1] == pytest.approx(ev.arrival_s[0] + 30 + leg / C.cruise_speed_ms)


def test_missed_deadline_makes_path_infeasible():
    d = drone("a", 37.4252, -122.1718)
    far = point("far", 37.4308, -122.1652, deadline_s=10)  # ~850 m away, 8 m/s
    assert evaluate_path(d, [far], C, CFG, 20.0) is None


def test_endurance_matches_the_safety_validator_exactly():
    """Bidding uses the same check_endurance as the safety gate, so it can
    never call a path feasible that the gate would block."""
    p = point("p", 37.4308, -122.1652)
    for battery in [20.5, 21.0, 22.0, 25.0, 40.0]:
        d = drone("a", 37.4252, -122.1718, battery=battery)
        feasible = evaluate_path(d, [p], C, CFG, 20.0) is not None
        gate_ok = not check_endurance(route_waypoints([p], 20.0), d.start, C, battery, "a")
        assert feasible == gate_ok


def test_point_needing_a_sensor_the_drone_lacks_gets_no_bid():
    d = drone("a", caps=["camera"])
    p = point("p", 37.4280, -122.1690, required_capability="thermal")
    assert not can_do(d, p)
    assert best_insertion(d, [], 0.0, p, C, CFG, 20.0) is None


def test_best_insertion_puts_a_point_where_it_costs_least():
    d = drone("a", 37.4260, -122.1690)
    near = point("near", 37.4265, -122.1690)
    far = point("far", 37.4300, -122.1690)
    path = [far]
    score = evaluate_path(d, path, C, CFG, 20.0).score
    gain, pos = best_insertion(d, path, score, near, C, CFG, 20.0)
    assert pos == 0 and gain > 0  # visit "near" on the way to "far"


@pytest.mark.parametrize(
    "bid_a,a,bid_b,b,expected",
    [
        (5.0, "d2", 4.0, "d1", True),  # higher bid wins
        (4.0, "d1", 5.0, "d2", False),
        (5.0, "d1", 5.0, "d2", True),  # tie -> lower id
        (5.0, "d2", 5.0, "d1", False),
        (0.1, "d9", 0.0, None, True),  # any positive bid beats nobody
        (0.0, "d1", 0.0, None, False),
    ],
)
def test_outbids_rule(bid_a, a, bid_b, b, expected):
    assert outbids(bid_a, a, bid_b, b) is expected


# --- comms graph & transport ----------------------------------------------------

def _drones():
    return [drone("a", 37.4270, -122.1700), drone("b", 37.4270, -122.1690), drone("c", 37.4270, -122.1650)]


def test_default_comms_is_fully_connected():
    g = CommsGraph.from_spec(CommsSpec(), _drones())
    assert g.neighbours("a") == ["b", "c"]
    assert g.diameter() == 1


def test_range_based_links():
    # a-b are ~90 m apart, b-c ~350 m.
    g = CommsGraph.from_spec(CommsSpec(range_m=200), _drones())
    assert g.neighbours("a") == ["b"]
    assert g.neighbours("c") == []
    assert g.components() == [["a", "b"], ["c"]]


def test_line_graph_diameter_and_link_cut():
    g = CommsGraph.from_spec(CommsSpec(links=[("a", "b"), ("b", "c")]), _drones())
    assert g.diameter() == 2
    g.set_link("b", "c", up=False)
    assert g.components() == [["a", "b"], ["c"]]
    g.set_link("b", "c", up=True)
    assert g.components() == [["a", "b", "c"]]


def test_link_to_unknown_drone_is_rejected():
    with pytest.raises(ValueError):
        CommsGraph.from_spec(CommsSpec(links=[("a", "zzz")]), _drones())


def test_links_and_range_together_are_rejected():
    with pytest.raises(ValueError):
        CommsSpec(links=[("a", "b")], range_m=100)


def test_transport_only_delivers_over_working_links_in_sender_order():
    g = CommsGraph.from_spec(CommsSpec(links=[("a", "b"), ("c", "b")]), _drones())
    t = InMemoryTransport(g)

    def msg(sender):
        return CbbaMessage(sender_id=sender, round=1, winning_bids={}, winners={}, timestamps={})

    t.send(msg("c"), "b")
    t.send(msg("a"), "b")
    t.send(msg("a"), "c")  # no a-c link: lost
    assert [m.sender_id for m in t.receive("b")] == ["a", "c"]
    assert t.receive("c") == []
    assert (t.sent, t.dropped) == (2, 1)
    assert t.receive("b") == []  # inbox emptied


def test_discount_must_be_a_valid_fraction():
    with pytest.raises(ValueError):
        CbbaConfig(discount_per_s=1.5)
    assert math.isclose(CbbaConfig().discount_per_s, 0.999)
