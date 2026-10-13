"""Round-by-round trace (how the drones decided) and re-planning from a
mid-mission position with a separate home point."""
from app.cbba.demo import sample_scenario
from app.cbba.models import DroneHealth
from app.cbba.service import allocate
from app.core.models import Waypoint
from tests.cbba.helpers import drone, point, scenario


def test_trace_records_bids_and_outbids_and_matches_the_result():
    trace = []
    r = allocate(sample_scenario("full"), trace=trace)
    assert len(trace) == r.rounds
    kinds = {(e.kind, e.reason) for t in trace for e in t.events}
    assert ("bid", None) in kinds
    assert ("dropped", "outbid") in kinds  # in the sample, drones do compete for points
    outbid = next(e for t in trace for e in t.events if e.reason == "outbid")
    assert outbid.winner and outbid.winner != outbid.drone and outbid.winner_bid >= outbid.bid
    assert trace[-1].claims == {p.drone_id: p.bundle for p in r.plans}
    assert trace[0].links == [("sim-1", "sim-2"), ("sim-1", "sim-3"), ("sim-2", "sim-3")]
    assert trace[0].messages == 6


def test_trace_shows_no_links_for_a_cut_off_drone():
    trace = []
    allocate(sample_scenario("split"), trace=trace)
    assert all("sim-3" not in link for t in trace for link in t.links)


def test_no_trace_unless_asked():
    assert allocate(sample_scenario("full")).converged  # trace=None is the default


def test_replan_from_mid_air_returns_to_home_not_to_current_position():
    home = Waypoint(lat=37.4260, lon=-122.1700, alt_m=0)
    here = Waypoint(lat=37.4290, lon=-122.1680, alt_m=20)
    d = drone("a", battery=80).model_copy(update={"start": here, "home": home})
    r = allocate(scenario([d], [point("p", 37.4295, -122.1675)]))
    plan = r.plans[0]
    assert (plan.route[0].lat, plan.route[0].lon) == (here.lat, here.lon)
    assert (plan.route[-1].lat, plan.route[-1].lon) == (home.lat, home.lon)
    assert r.dispatch_allowed


def test_replan_endurance_is_conservative_never_optimistic():
    """Mid-mission, the battery check also counts home -> current position
    (already flown). So a drone that would just make it if starting fresh
    at its current position may be refused -- never the other way round."""
    home = Waypoint(lat=37.4252, lon=-122.1718, alt_m=0)
    here = Waypoint(lat=37.4305, lon=-122.1655, alt_m=20)
    p = point("p", 37.4306, -122.1656)
    at_home_fresh = drone("a", 37.4305, -122.1655, battery=21.3)
    mid_air = at_home_fresh.model_copy(update={"home": home})
    fresh = allocate(scenario([drone("a", 37.4305, -122.1655, battery=21.3)], [p]))
    replan = allocate(scenario([mid_air], [p]))
    # Starting there fresh (home == start) the tiny hop is fine...
    assert fresh.winners == {"p": "a"}
    # ...but mid-mission, getting back home from there needs more than the reserve allows.
    assert replan.winners == {"p": None}
    assert replan.dispatch_allowed  # nothing unsafe was assigned


def test_home_outside_fence_excludes_the_drone():
    d = drone("a").model_copy(update={"home": Waypoint(lat=37.4400, lon=-122.1700, alt_m=0)})
    r = allocate(scenario([d], [point("p", 37.4280, -122.1690)]))
    assert [(x.item_id, x.reason) for x in r.excluded_drones] == [("a", "its home is outside the geofence")]


def test_offline_drone_mid_mission_points_go_to_others():
    sc = sample_scenario("full")
    first = allocate(sc)
    lost = next(p for p in first.plans if p.drone_id == "sim-2").path
    drones = [d.model_copy(update={"health": DroneHealth.OFFLINE}) if d.drone_id == "sim-2" else d for d in sc.drones]
    second = allocate(sc.model_copy(update={"drones": drones}))
    assert all(second.winners[p] in ("sim-1", "sim-3") for p in lost)
