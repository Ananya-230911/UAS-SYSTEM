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


def survey_box(view: MapView) -> tuple[float, float, float, float]:
    """(south, west, north, east) of the survey area: centred on the
    view, each side clamped to [MIN_SIDE_M, MAX_SIDE_M]."""
    lat0 = (view.south + view.north) / 2
    lon0 = (view.west + view.east) / 2
    h_m = min(MAX_SIDE_M, max(MIN_SIDE_M, (view.north - view.south) * M_PER_DEG_LAT))
    w_m = min(MAX_SIDE_M, max(MIN_SIDE_M, (view.east - view.west) * m_per_deg_lon(lat0)))
    dlat = h_m / 2 / M_PER_DEG_LAT
    dlon = w_m / 2 / m_per_deg_lon(lat0)
    return lat0 - dlat, lon0 - dlon, lat0 + dlat, lon0 + dlon


def _rect(s: float, w: float, n: float, e: float) -> list[GeofencePoint]:
    return [GeofencePoint(lat=s, lon=w), GeofencePoint(lat=s, lon=e), GeofencePoint(lat=n, lon=e), GeofencePoint(lat=n, lon=w)]


def build_survey_scenario(view: MapView, num_drones: int = DEFAULT_DRONES, altitude_m: float | None = None) -> Scenario:
    """Deterministic: same view and numbers -> same scenario."""
    if not 1 <= num_drones <= MAX_DRONES:
        raise ValueError(f"num_drones must be 1-{MAX_DRONES}")
    s, w, n, e = survey_box(view)
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


def plan_survey(req: SurveyRequest, provider=None) -> SurveyPlan:
    """One line + map view -> a survey Scenario, or NeedsClarification."""
    if _TARGETING.search(req.text):
        raise NeedsClarification(OUT_OF_SCOPE_ANSWER)
    if provider is None and req.engine == "rules":
        provider = RuleBasedGoalParser()
    parsed, provider_name = interpret_goal(req.text, provider)
    num = parsed.num_vehicles or DEFAULT_DRONES
    if num > MAX_DRONES:
        raise NeedsClarification(f"This simulation flies at most {MAX_DRONES} drones. Try e.g. '{MAX_DRONES} drones survey this area'.")
    scenario = build_survey_scenario(req.view, num, parsed.altitude_m)
    s, w, n, e = survey_box(req.view)
    lat0 = (s + n) / 2
    size = f"{(e - w) * m_per_deg_lon(lat0) / 1000:.1f} x {(n - s) * M_PER_DEG_LAT / 1000:.1f} km"
    return SurveyPlan(provider=provider_name, scenario=scenario, area=size)


# --- camera ------------------------------------------------------------------------

class CameraRequest(BaseModel):
    drone_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    point_id: str = Field(min_length=1, max_length=64)


class CameraResult(BaseModel):
    point_id: str
    drone_id: str
    # "webcam": a live picture from the webcam, taken now. "sample photo":
    # the perception service runs without the webcam. "unavailable": no
    # camera answered -- counts is empty, nothing is guessed.
    source: Literal["webcam", "sample photo", "unavailable"]
    counts: dict[str, int]
    image: str | None = None  # data:image/jpeg;base64,... with the boxes drawn
    note: str


START_CAMERA = r"powershell -ExecutionPolicy Bypass -File scripts\run_perception.ps1 -Webcam"


def camera_look(req: CameraRequest, http: httpx.Client | None = None, base_url: str | None = None) -> CameraResult:
    """Ask the perception service for a picture taken now + YOLOv8 on it.
    GET only. Never invents a result."""
    base_url = base_url or PERCEPTION_URL
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
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return CameraResult(
            point_id=req.point_id, drone_id=req.drone_id, source="unavailable", counts={},
            note=f"Camera not connected ({base_url}: {type(exc).__name__}). Start it with: {START_CAMERA}",
        )
    finally:
        if http is None:
            client.close()
