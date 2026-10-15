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
    MAX_SIDE_M, CameraRequest, MapView, SurveyRequest, build_survey_scenario, camera_look, plan_survey, survey_box,
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
# No made-up results anywhere: either a real picture + YOLOv8, or "unavailable".

def _req(**kw):
    return CameraRequest(**{"drone_id": "D1", "point_id": "S01", "mode": "webcam", **kw})


def _perception(camera="webcam", detections=("person", "person", "cup")):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"frame": "webcam_frame.jpg", "camera": camera, "image_jpeg_b64": "SU1H",
                                         "detections": [{"class_name": c} for c in detections]})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_camera_takes_a_live_webcam_picture():
    http, seen = _perception()
    r = camera_look(_req(), http=http)
    assert r.source == "webcam" and r.counts == {"person": 2, "cup": 1}
    assert r.image == "data:image/jpeg;base64,SU1H" and "Live picture from your webcam" in r.note
    assert seen == [("GET", "/vehicles/D1/perception", {"image": "true"})]   # read only, picture requested


def test_camera_says_when_it_is_a_sample_photo():
    http, _ = _perception(camera="sample photo", detections=())
    r = camera_look(_req(), http=http)
    assert r.source == "sample photo" and r.counts == {} and "-Webcam" in r.note


def test_camera_not_connected_invents_nothing():
    def down(request):
        raise httpx.ConnectError("refused")

    r = camera_look(_req(), http=httpx.Client(transport=httpx.MockTransport(down)))
    assert r.source == "unavailable" and r.counts == {} and r.image is None
    assert "Camera not connected" in r.note and "run_perception.ps1 -Webcam" in r.note


def test_camera_error_shows_the_reason():
    def busy(request):
        return httpx.Response(503, json={"detail": "RuntimeError: failed to read a frame from the webcam"})

    r = camera_look(_req(), http=httpx.Client(transport=httpx.MockTransport(busy)))
    assert r.source == "unavailable" and r.counts == {}
    assert "error 503" in r.note and "failed to read a frame from the webcam" in r.note


def test_camera_endpoint_without_a_camera_service(monkeypatch):
    import app.survey as survey_mod

    monkeypatch.setattr(survey_mod, "PERCEPTION_URL", "http://127.0.0.1:9")   # nothing listens there
    body = client.post("/survey/camera", json={"drone_id": "D1", "point_id": "S01", "mode": "webcam"}).json()
    assert body["source"] == "unavailable" and body["counts"] == {}
    assert client.post("/survey/camera", json={"drone_id": "../x", "point_id": "S01", "mode": "webcam"}).status_code == 422
    # geo is the default and needs the point's position
    assert client.post("/survey/camera", json={"drone_id": "D1", "point_id": "S01"}).status_code == 422
    geo = client.post("/survey/camera", json={"drone_id": "D1", "point_id": "S01", "lat": 12.8231, "lon": 80.0442}).json()
    assert geo["source"] == "unavailable" and geo["counts"] == {} and "Vision service not reachable" in geo["note"]


# --- real geographic imagery (ADR-0022) ---------------------------------------------

def _geo_req():
    return CameraRequest(drone_id="D2", point_id="S04", lat=12.8231, lon=80.0442)


def test_geo_mode_asks_for_the_points_real_image():
    seen = []
    report = {"imagery": {"provider": "Copernicus Sentinel-2 L2A (ESA), via Element 84 Earth Search", "capture_date": "2026-09-30",
                          "gsd_m": 10.0}, "counts": {}, "image_jpeg_b64": "SU1H", "models_run": []}

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=report)

    r = camera_look(_geo_req(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen == [("GET", "/geo/inspect", {"lat": "12.8231", "lon": "80.0442", "radius_m": "100.0"})]
    assert r.source == "geo" and r.report == report and r.image == "data:image/jpeg;base64,SU1H"
    assert "Copernicus Sentinel-2 L2A (ESA)" in r.note and "2026-09-30" in r.note and "not live footage" in r.note


def test_geo_mode_no_image_is_reported_not_replaced():
    detail = {"message": "No openly licensed imagery could be retrieved for this point.",
              "attempts": [{"source": "OpenAerialMap", "result": "no OpenAerialMap imagery covers this point"},
                           {"source": "Sentinel-2", "result": "no Sentinel-2 scene with under 40% cloud covers this point"}]}
    r = camera_look(_geo_req(), http=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"detail": detail}))))
    assert r.source == "unavailable" and r.image is None and r.counts == {}
    assert "no OpenAerialMap imagery" in r.note and "Nothing was substituted" in r.note


def test_survey_endpoint():
    lat, lon = PLACES["Delhi"]
    v = {"south": lat - 0.004, "west": lon - 0.006, "north": lat + 0.004, "east": lon + 0.006}
    body = client.post("/survey/plan", json={"text": "3 drones survey this area", "view": v, "engine": "rules"}).json()
    assert body["question"] is None and len(body["scenario"]["drones"]) == 3 and body["area"]
    q = client.post("/survey/plan", json={"text": "attack it", "view": v}).json()
    assert q["scenario"] is None and "outside" in q["question"]


# --- a place named in the line (operator: "3 drones survey sector 15 area") ----------

FARIDABAD_VIEW = MapView(south=28.385, west=77.285, north=28.425, east=77.335)   # what was on screen
SECTOR15 = {"lat": "28.3963", "lon": "77.3237", "display_name": "Sector 15, Faridabad, Haryana, India",
            "boundingbox": ["28.3915", "28.4011", "77.3180", "77.3294"]}


def _nominatim(hits_bounded, hits_anywhere=None):
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        hits = hits_bounded if params["bounded"] == "1" else (hits_anywhere or [])
        return httpx.Response(200, json=hits)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_named_place_is_looked_up_near_the_map_and_surveyed():
    http, calls = _nominatim([SECTOR15])
    plan = plan_survey(SurveyRequest(text="3 drones survey sector 15 area", view=FARIDABAD_VIEW, engine="rules"), http=http)
    assert calls[0]["q"] == "sector 15" and calls[0]["bounded"] == "1"
    w, n, e, s = (float(x) for x in calls[0]["viewbox"].split(","))
    assert s < 28.3963 < n and w < 77.3237 < e                     # searched around the map on screen
    assert plan.place["name"].startswith("Sector 15, Faridabad") and plan.place["near_map"] is True
    lats = [p.lat for p in plan.scenario.points]
    lons = [p.lon for p in plan.scenario.points]
    assert sum(lats) / len(lats) == pytest.approx(28.3963, abs=1e-4)   # survey centred on Sector 15,
    assert sum(lons) / len(lons) == pytest.approx(77.3237, abs=1e-4)   # not on the middle of the view
    assert allocate(plan.scenario).dispatch_allowed


def test_place_not_near_the_map_is_searched_anywhere():
    iit = {"lat": "28.5450", "lon": "77.1926", "display_name": "IIT Delhi, New Delhi", "boundingbox": ["28.54", "28.55", "77.18", "77.20"]}
    http, calls = _nominatim([], [iit])
    plan = plan_survey(SurveyRequest(text="3 drones survey IIT Delhi", view=FARIDABAD_VIEW, engine="rules"), http=http)
    assert [c["bounded"] for c in calls] == ["1", "0"] and plan.place["near_map"] is False


def test_unknown_place_asks_instead_of_guessing():
    http, _ = _nominatim([], [])
    with pytest.raises(NeedsClarification, match="couldn't find 'Narnia sector 99'"):
        plan_survey(SurveyRequest(text="survey Narnia sector 99", view=FARIDABAD_VIEW, engine="rules"), http=http)


def test_this_area_never_looks_anything_up():
    http, calls = _nominatim([SECTOR15])
    plan = plan_survey(SurveyRequest(text="3 drones survey this area", view=FARIDABAD_VIEW, engine="rules"), http=http)
    assert calls == [] and plan.place is None


def test_ai_gives_the_name_only_the_lookup_gives_the_position():
    class FakeGroq:
        name = "groq (fake)"

        def parse(self, _):
            return ParsedGoal(intent="area_search", num_vehicles=2, place_name="sector 15")

    http, calls = _nominatim([SECTOR15])
    plan = plan_survey(SurveyRequest(text="please send two drones to look at sector 15", view=FARIDABAD_VIEW), provider=FakeGroq(), http=http)
    assert calls[0]["q"] == "sector 15" and len(plan.scenario.drones) == 2
    assert "lat" not in ParsedGoal.model_fields and "lon" not in ParsedGoal.model_fields   # the AI cannot give coordinates
