"""Phase F: one line + any place on the map -> survey mission -> CBBA ->
live agents -> camera results (docs/adr/0021-one-line-survey.md)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.cbba.service import allocate
from app.mission.goal_parser import NeedsClarification, ParsedGoal
from app.planning.coverage import M_PER_DEG_LAT, m_per_deg_lon
from app.safety.geofence import point_in_polygon
from app.server import app
from app.sim.models import FailureEvent
from app.sim.world import run_simulation
from app.survey import (
    MAX_SIDE_M, CameraRequest, MapView, SurveyRequest, build_survey_scenario, camera_look, plan_survey,
    simulated_counts, survey_box,
)

client = TestClient(app)

# Map views (roughly what Leaflet shows after a search, zoomed in) around
# the world: both hemispheres, near the equator, high latitude.
PLACES = {
    "Delhi": (28.6139, 77.2090),
    "Mumbai": (19.0760, 72.8777),
    "London": (51.5074, -0.1278),
    "Sydney": (-33.8688, 151.2093),
    "Nairobi": (-1.2921, 36.8219),
    "Oslo": (59.9139, 10.7522),
}


def view(lat, lon, half_lat=0.004, half_lon=0.006):
    return MapView(south=lat - half_lat, west=lon - half_lon, north=lat + half_lat, east=lon + half_lon)


def survey(text, v, engine="rules"):
    return plan_survey(SurveyRequest(text=text, view=v, engine=engine))


# --- one line -> scenario ------------------------------------------------------------

def test_three_drones_survey_this_area():
    plan = survey("3 drones survey this area", view(*PLACES["Delhi"]))
    sc = plan.scenario
    assert [d.drone_id for d in sc.drones] == ["D1", "D2", "D3"]
    assert len(sc.points) == 12 and sc.points[0].point_id == "S01"
    assert plan.provider == "rule-based"
    fence = [p.model_dump() for p in sc.geofence.points]
    assert all(point_in_polygon(p.lat, p.lon, fence) for p in sc.points)
    assert all(point_in_polygon(d.start.lat, d.start.lon, fence) for d in sc.drones)


@pytest.mark.parametrize("text, drones, alt", [
    ("survey this area with 5 drones at 40 m", 5, 40.0),
    ("check this place", 3, 20.0),          # no number -> 3 drones, default altitude
    ("two drones, look over this area", 2, 20.0),
    ("monitor the area with 1 drone", 1, 20.0),
])
def test_numbers_and_defaults(text, drones, alt):
    sc = survey(text, view(*PLACES["Mumbai"])).scenario
    assert len(sc.drones) == drones and sc.base_altitude_m == alt
    assert len(sc.points) >= 6


def test_unclear_or_too_many_drones_asks():
    with pytest.raises(NeedsClarification):
        survey("hello drones", view(*PLACES["London"]))
    with pytest.raises(NeedsClarification, match="at most 8"):
        survey("survey this area with 9 drones", view(*PLACES["London"]))


@pytest.mark.parametrize("text", ["attack this area", "find targets here", "survey and destroy the enemy", "bomb this place"])
def test_out_of_scope_is_refused_before_any_ai(text):
    class Boom:
        name = "must-not-run"

        def parse(self, _):
            raise AssertionError("the AI must not be called")

    with pytest.raises(NeedsClarification, match="outside what this system does"):
        plan_survey(SurveyRequest(text=text, view=view(*PLACES["Delhi"])), provider=Boom())


def test_ai_only_gives_numbers_never_positions():
    class FakeGroq:
        name = "groq (fake)"

        def parse(self, _):
            return ParsedGoal(intent="area_search", num_vehicles=4, altitude_m=30)

    v = view(*PLACES["Sydney"])
    plan = plan_survey(SurveyRequest(text="scan it", view=v), provider=FakeGroq())
    assert plan.provider == "groq (fake)" and len(plan.scenario.drones) == 4
    # Positions come only from the map view -- same as the rules path.
    assert plan.scenario.points == build_survey_scenario(v, 4, 30).points


def test_big_view_is_clamped_to_the_middle():
    lat, lon = PLACES["Delhi"]
    big = view(lat, lon, half_lat=0.2, half_lon=0.3)   # a whole-city view, ~60 km wide
    s, w, n, e = survey_box(big)
    assert (n - s) * M_PER_DEG_LAT == pytest.approx(MAX_SIDE_M)
    assert (e - w) * m_per_deg_lon(lat) == pytest.approx(MAX_SIDE_M)
    assert (s + n) / 2 == pytest.approx(lat) and (w + e) / 2 == pytest.approx(lon)


def test_deterministic():
    v = view(*PLACES["Oslo"])
    assert build_survey_scenario(v, 3) == build_survey_scenario(v, 3)


def test_bad_view_is_rejected():
    assert client.post("/survey/plan", json={"text": "survey", "view": {"south": 2, "west": 0, "north": 1, "east": 1}}).status_code == 422


# --- the whole chain, anywhere --------------------------------------------------------

@pytest.mark.parametrize("place", sorted(PLACES))
def test_any_place_plans_safely_and_flies_every_point(place):
    sc = survey("3 drones survey this area", view(*PLACES[place])).scenario
    res = allocate(sc)
    assert res.dispatch_allowed, res.safety.issues
    assigned = [p for plan in res.plans for p in plan.path]
    assert sorted(assigned) == sorted(p.point_id for p in sc.points)   # every point once, no repeats
    sim = run_simulation(sc)
    assert sim.summary["inspected_count"] == len(sc.points)
    assert not sim.summary["double_inspected"]
    # The drones negotiated over the radio before flying.
    talk = {pair for f in sim.frames for pair in f.talk}
    assert {("D1", "D2"), ("D2", "D1"), ("D2", "D3")} <= talk


def test_drone_lost_mid_survey_others_take_over():
    sc = survey("3 drones survey this area", view(*PLACES["Delhi"])).scenario
    sim = run_simulation(sc, events=[FailureEvent(at_s=60, kind="drone_lost", drone_id="D2")])
    assert sim.summary["planning_rounds"] >= 2   # the others noticed and re-planned
    assert not [e for e in sim.events if e.kind == "inspect" and e.drone == "D2" and e.t > 60]
    assert sim.summary["inspected_count"] == len(sc.points)   # D1 and D3 finished D2's share


def test_instructions_work_on_a_survey_mission():
    """Phase D on top: 'S3 is high priority, D2 is offline' (S3 = S03)."""
    sc = survey("3 drones survey this area", view(*PLACES["Mumbai"])).scenario.model_dump(mode="json")
    body = client.post("/cbba/instruct", json={"scenario": sc, "text": "S3 is high priority, skip S5, D2 is offline", "engine": "rules"}).json()
    assert [a["change"] for a in body["accepted"]] == [
        "Make S03 high priority", "Leave S05 out of this mission", "Take D2 out of the mission (offline)",
    ]


# --- camera -------------------------------------------------------------------------

def _req(**kw):
    return CameraRequest(**{"drone_id": "D1", "point_id": "S01", "lat": 28.61, "lon": 77.2, **kw})


def test_camera_uses_the_perception_service_when_it_answers():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"frame": "frame_a.jpg", "detections": [
            {"class_name": "person"}, {"class_name": "person"}, {"class_name": "car"}]})

    r = camera_look(_req(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert r.source == "perception" and r.counts == {"person": 2, "car": 1}
    assert "frame_a.jpg" in r.note and "not a picture of this place" in r.note
    assert seen == [("GET", "/vehicles/D1/perception")]   # read only


def test_camera_falls_back_to_labelled_simulation():
    def down(request):
        raise httpx.ConnectError("refused")

    r = camera_look(_req(), http=httpx.Client(transport=httpx.MockTransport(down)))
    assert r.source == "simulated" and "made-up" in r.note
    assert r.counts == simulated_counts(28.61, 77.2)
    assert simulated_counts(28.61, 77.2) == simulated_counts(28.61, 77.2)   # same place, same answer


def test_camera_endpoint_simulated():
    body = client.post("/survey/camera", json={"drone_id": "D1", "point_id": "S01", "lat": 19.07, "lon": 72.87, "source": "simulated"}).json()
    assert body["source"] == "simulated" and body["point_id"] == "S01"
    assert client.post("/survey/camera", json={"drone_id": "../x", "point_id": "S01", "lat": 0, "lon": 0}).status_code == 422


def test_survey_endpoint():
    lat, lon = PLACES["Delhi"]
    v = {"south": lat - 0.004, "west": lon - 0.006, "north": lat + 0.004, "east": lon + 0.006}
    body = client.post("/survey/plan", json={"text": "3 drones survey this area", "view": v, "engine": "rules"}).json()
    assert body["question"] is None and len(body["scenario"]["drones"]) == 3 and body["area"]
    q = client.post("/survey/plan", json={"text": "attack it", "view": v}).json()
    assert q["scenario"] is None and "outside" in q["question"]
