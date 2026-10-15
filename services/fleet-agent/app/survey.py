"""One-line survey (Phase F, docs/adr/0021-one-line-survey.md).

The operator searches any place on the map and types one line, e.g.
"3 drones survey this area". This module turns that into a CBBA
Scenario -- nothing else:

  - The line is read by the existing goal parser (app/mission/
    goal_parser.py: Groq if GROQ_API_KEY is set, else rules), which may
    only say how many drones and at what altitude. It never sees
    coordinates, places points or picks routes.
  - Where to look is the map area on the operator's screen. The survey
    points are the centres of the existing coverage grid
    (app/planning/coverage.py::split_into_cells) over that area; drones,
    launch spot and fence are placed by fixed rules below.
  - Who flies where is then CBBA + the safety gate, exactly as for a
    hand-made mission (app/cbba/service.py).

It also answers "what did the camera see at this point" for the map: it
asks the perception service to take a picture NOW (the webcam if that
service runs with PERCEPTION_USE_WEBCAM=1) and run YOLOv8 on it, and
returns the counts plus the picture with boxes. If no camera answers it
says so -- it never makes up a result. Display only: a detection is shown
to the operator and nothing reacts to it.

Simulation only. Nothing here talks to a drone.
"""
from __future__ import annotations

import os
from collections import Counter
from typing import Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from app.cbba.models import DroneSpec, InspectionPoint, Scenario
from app.core.models import Geofence, GeofencePoint, SearchArea, Waypoint
from app.mission.goal_parser import NeedsClarification, RuleBasedGoalParser, interpret_goal
from app.planning.coverage import M_PER_DEG_LAT, m_per_deg_lon, split_into_cells
from app.supervisor.instructions import OUT_OF_SCOPE_ANSWER, _TARGETING

DEFAULT_DRONES = 3
MAX_DRONES = 8
# The survey square is the middle of the map view, at most this big per
# side: a zoomed-out city view would otherwise be a 10 km mission.
MAX_SIDE_M = 1200.0
MIN_SIDE_M = 100.0
POINTS_PER_DRONE = 4
MIN_POINTS, MAX_POINTS = 6, 24
FENCE_PAD_M = 60.0

PERCEPTION_URL = os.environ.get("PERCEPTION_URL", "http://localhost:8010")


class MapView(BaseModel):
    """The map area on the operator's screen (Leaflet getBounds())."""

    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def _ordered(self) -> MapView:
        if not (self.south < self.north and self.west < self.east):
            raise ValueError("the map view must have south < north and west < east")
        return self


class SurveyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    view: MapView
    engine: Literal["auto", "rules"] = "auto"


class SurveyPlan(BaseModel):
    provider: str
    scenario: Scenario
    area: str  # plain-English description, e.g. "1.2 x 0.8 km"
    place: dict | None = None  # the named place as OpenStreetMap found it, or None (map view)


NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
USER_AGENT = "UAS-SYSTEM student project (simulation; survey place lookup)"


def find_place(name: str, view: MapView, http: httpx.Client) -> dict | None:
    """Look a place name up on OpenStreetMap (Nominatim), preferring matches
    near the map on screen: many cities have a "Sector 15". Deterministic
    lookup -- the AI only supplied the name as written."""
    lat0, lon0 = (view.south + view.north) / 2, (view.west + view.east) / 2
    # Search box: the view, but at least ~30 km across.
    half_lat = max((view.north - view.south) / 2, 15_000 / M_PER_DEG_LAT)
    half_lon = max((view.east - view.west) / 2, 15_000 / m_per_deg_lon(lat0))
    near = f"{lon0 - half_lon},{lat0 + half_lat},{lon0 + half_lon},{lat0 - half_lat}"
    for bounded in ("1", "0"):  # near the map first, then anywhere
        resp = http.get(f"{NOMINATIM_URL}/search", params={"q": name, "format": "jsonv2", "limit": 1,
                                                          "viewbox": near, "bounded": bounded})
        resp.raise_for_status()
        hits = resp.json()
        if hits:
            h = hits[0]
            lat, lon = float(h["lat"]), float(h["lon"])
            bb = [float(x) for x in h.get("boundingbox", [lat, lat, lon, lon])]  # south, north, west, east
            return {"query": name, "name": h.get("display_name", name), "lat": lat, "lon": lon,
                    "w_m": (bb[3] - bb[2]) * m_per_deg_lon(lat), "h_m": (bb[1] - bb[0]) * M_PER_DEG_LAT,
                    "near_map": bounded == "1", "source": "OpenStreetMap Nominatim"}
    return None


def box_at(lat0: float, lon0: float, w_m: float, h_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a w_m x h_m area centred on the point,
    each side clamped to [MIN_SIDE_M, MAX_SIDE_M]."""
    h_m = min(MAX_SIDE_M, max(MIN_SIDE_M, h_m))
    w_m = min(MAX_SIDE_M, max(MIN_SIDE_M, w_m))
    dlat = h_m / 2 / M_PER_DEG_LAT
    dlon = w_m / 2 / m_per_deg_lon(lat0)
    return lat0 - dlat, lon0 - dlon, lat0 + dlat, lon0 + dlon


def survey_box(view: MapView) -> tuple[float, float, float, float]:
    """The middle of the map view (used when the line names no place)."""
    lat0 = (view.south + view.north) / 2
    lon0 = (view.west + view.east) / 2
    return box_at(lat0, lon0, (view.east - view.west) * m_per_deg_lon(lat0), (view.north - view.south) * M_PER_DEG_LAT)


def _rect(s: float, w: float, n: float, e: float) -> list[GeofencePoint]:
    return [GeofencePoint(lat=s, lon=w), GeofencePoint(lat=s, lon=e), GeofencePoint(lat=n, lon=e), GeofencePoint(lat=n, lon=w)]


def build_survey_scenario(view: MapView, num_drones: int = DEFAULT_DRONES, altitude_m: float | None = None,
                          box: tuple[float, float, float, float] | None = None) -> Scenario:
    """Deterministic: same area and numbers -> same scenario. `box` (south,
    west, north, east) overrides the map view (a named place)."""
    if not 1 <= num_drones <= MAX_DRONES:
        raise ValueError(f"num_drones must be 1-{MAX_DRONES}")
    s, w, n, e = box or survey_box(view)
    lat0 = (s + n) / 2

    cells = split_into_cells(SearchArea(points=_rect(s, w, n, e)), min(MAX_POINTS, max(MIN_POINTS, POINTS_PER_DRONE * num_drones)))
    points = [
        InspectionPoint(point_id=f"S{i:02d}", lat=round(c.centroid[0], 6), lon=round(c.centroid[1], 6))
        for i, c in enumerate(cells, start=1)
    ]

    # Launch spots spread evenly along the area's south edge, so each
    # drone starts near its own part of the area (and the radio links
    # between them are visible on the map).
    drones = [
        DroneSpec(
            drone_id=f"D{i + 1}",
            start=Waypoint(lat=round(s, 6), lon=round(w + (i + 0.5) * (e - w) / num_drones, 6), alt_m=0),
            battery_pct=100,
            capabilities=["camera"],
        )
        for i in range(num_drones)
    ]

    pad_lat = FENCE_PAD_M / M_PER_DEG_LAT
    pad_lon = FENCE_PAD_M / m_per_deg_lon(lat0)
    fence = Geofence(points=_rect(s - pad_lat, w - pad_lon, n + pad_lat, e + pad_lon))
    extra = {"base_altitude_m": altitude_m} if altitude_m else {}
    return Scenario(drones=drones, points=points, geofence=fence, **extra)


def plan_survey(req: SurveyRequest, provider=None, http: httpx.Client | None = None) -> SurveyPlan:
    """One line + map view -> a survey Scenario, or NeedsClarification.
    A place named in the line ("survey sector 15") is looked up and
    surveyed; otherwise the middle of the map view is."""
    if _TARGETING.search(req.text):
        raise NeedsClarification(OUT_OF_SCOPE_ANSWER)
    if provider is None and req.engine == "rules":
        provider = RuleBasedGoalParser()
    parsed, provider_name = interpret_goal(req.text, provider)
    num = parsed.num_vehicles or DEFAULT_DRONES
    if num > MAX_DRONES:
        raise NeedsClarification(f"This simulation flies at most {MAX_DRONES} drones. Try e.g. '{MAX_DRONES} drones survey this area'.")
    place = None
    box = None
    if parsed.place_name:
        client = http or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), headers={"User-Agent": USER_AGENT})
        try:
            place = find_place(parsed.place_name, req.view, client)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            raise NeedsClarification(
                f"I couldn't look up '{parsed.place_name}' on OpenStreetMap ({type(exc).__name__}). Search for it in the box "
                "above the map, then say 'survey this area'.") from exc
        finally:
            if http is None:
                client.close()
        if place is None:
            raise NeedsClarification(
                f"I couldn't find '{parsed.place_name}' on OpenStreetMap. Check the spelling, or search for it in the box "
                "above the map and say 'survey this area'.")
        box = box_at(place["lat"], place["lon"], place["w_m"], place["h_m"])
    scenario = build_survey_scenario(req.view, num, parsed.altitude_m, box=box)
    s, w, n, e = box or survey_box(req.view)
    lat0 = (s + n) / 2
    size = f"{(e - w) * m_per_deg_lon(lat0) / 1000:.1f} x {(n - s) * M_PER_DEG_LAT / 1000:.1f} km"
    return SurveyPlan(provider=provider_name, scenario=scenario, area=size, place=place)


# --- camera ------------------------------------------------------------------------

class CameraRequest(BaseModel):
    drone_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    point_id: str = Field(min_length=1, max_length=64)
    # "geo" (default): a REAL image of the point retrieved from an openly
    # licensed imagery source (perception GET /geo/inspect, ADR-0022).
    # "webcam": the old demo -- this computer's webcam, not the place.
    mode: Literal["geo", "webcam"] = "geo"
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_m: float = Field(default=100.0, ge=20, le=1000)

    @model_validator(mode="after")
    def _geo_needs_position(self) -> CameraRequest:
        if self.mode == "geo" and (self.lat is None or self.lon is None):
            raise ValueError("mode 'geo' needs lat and lon")
        return self


class CameraResult(BaseModel):
    point_id: str
    drone_id: str
    # "webcam": a live picture from the webcam, taken now. "sample photo":
    # the perception service runs without the webcam. "unavailable": no
    # camera answered -- counts is empty, nothing is guessed.
    # "geo": retrieved geographic imagery of the point (never live footage).
    source: Literal["geo", "webcam", "sample photo", "unavailable"]
    counts: dict[str, int]
    image: str | None = None  # data:image/jpeg;base64,... with the boxes drawn
    note: str
    report: dict | None = None  # geo: the full perception /geo/inspect report (source, date, land cover, OSM, limits)


START_CAMERA = r"powershell -ExecutionPolicy Bypass -File scripts\run_perception.ps1 -Webcam"


def camera_look(req: CameraRequest, http: httpx.Client | None = None, base_url: str | None = None) -> CameraResult:
    """Ask the perception service for a picture taken now + YOLOv8 on it.
    GET only. Never invents a result."""
    base_url = base_url or PERCEPTION_URL
    if req.mode == "geo":
        return _geo_look(req, http, base_url)
    client = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=2.0))
    try:
        resp = client.get(f"{base_url}/vehicles/{req.drone_id}/perception", params={"image": "true"})
        resp.raise_for_status()
        body = resp.json()
        counts = dict(Counter(str(d["class_name"]) for d in body["detections"]))
        source = "webcam" if body.get("camera") == "webcam" else "sample photo"
        b64 = body.get("image_jpeg_b64")
        note = (
            "Live picture from your webcam, taken when the drone reached this place; real YOLOv8 detection."
            if source == "webcam"
            else "Real YOLOv8 detection, but on a bundled sample photo: the camera service was started without "
            f"the webcam. For your webcam: {START_CAMERA}"
        )
        return CameraResult(
            point_id=req.point_id, drone_id=req.drone_id, source=source, counts=counts,
            image=f"data:image/jpeg;base64,{b64}" if b64 else None, note=note,
        )
    except httpx.HTTPStatusError as exc:
        # The camera service is running but could not take the picture
        # (no webcam, webcam busy, YOLO not installed ...): show its reason.
        try:
            why = exc.response.json().get("detail", "")
        except ValueError:
            why = exc.response.text[:200]
        return CameraResult(
            point_id=req.point_id, drone_id=req.drone_id, source="unavailable", counts={},
            note=f"The camera service answered with error {exc.response.status_code}: {why or 'no details'}. "
            "Check the camera service window.",
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return CameraResult(
            point_id=req.point_id, drone_id=req.drone_id, source="unavailable", counts={},
            note=f"Camera not connected ({base_url}: {type(exc).__name__}). Start it with: {START_CAMERA}",
        )
    finally:
        if http is None:
            client.close()


START_VISION = r"powershell -ExecutionPolicy Bypass -File scripts\run_perception.ps1"


def _geo_look(req: CameraRequest, http: httpx.Client | None, base_url: str) -> CameraResult:
    """Real image of the point (OpenAerialMap, else Sentinel-2) + analysis,
    from the perception service. GET only. A missing image is reported as
    such -- never replaced."""
    client = http or httpx.Client(timeout=httpx.Timeout(90.0, connect=2.0))
    try:
        resp = client.get(f"{base_url}/geo/inspect", params={"lat": req.lat, "lon": req.lon, "radius_m": req.radius_m})
        if resp.status_code == 404:
            detail = resp.json().get("detail", {})
            tried = "; ".join(f"{a.get('source')}: {a.get('result')}" for a in detail.get("attempts", []))
            return CameraResult(point_id=req.point_id, drone_id=req.drone_id, source="unavailable", counts={},
                                note=f"No real image is available for this place ({tried}). Nothing was substituted.")
        resp.raise_for_status()
        rep = resp.json()
        b64 = rep.get("image_jpeg_b64")
        im = rep.get("imagery", {})
        note = (f"Retrieved image from {str(im.get('provider', '')).split(',')[0]}"
                f"{', captured ' + im['capture_date'] if im.get('capture_date') else ''}, {im.get('gsd_m')} m per pixel - "
                "not live footage from the simulated drone.")
        return CameraResult(point_id=req.point_id, drone_id=req.drone_id, source="geo", counts=rep.get("counts", {}),
                            image=f"data:image/jpeg;base64,{b64}" if b64 else None, note=note, report=rep)
    except httpx.HTTPStatusError as exc:
        try:
            why = exc.response.json().get("detail", "")
        except ValueError:
            why = exc.response.text[:200]
        return CameraResult(point_id=req.point_id, drone_id=req.drone_id, source="unavailable", counts={},
                            note=f"The vision service answered with error {exc.response.status_code}: {why or 'no details'}.")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return CameraResult(point_id=req.point_id, drone_id=req.drone_id, source="unavailable", counts={},
                            note=f"Vision service not reachable ({base_url}: {type(exc).__name__}). Start it with: {START_VISION}")
    finally:
        if http is None:
            client.close()
