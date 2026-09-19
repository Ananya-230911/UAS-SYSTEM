from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session, init_db
from .geofence import point_in_polygon, waypoints_outside_fence
from .models import Command, EventLogEntry, Geofence, Mission, TelemetrySample, Vehicle
from .schemas import (
    CommandCreate,
    CommandOut,
    GeofenceCreate,
    GeofenceOut,
    MissionCreate,
    MissionOut,
    TelemetryIngest,
    TelemetryOut,
    VehicleOut,
)
from .ws_manager import ws_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="UAS-SYSTEM API", version="0.1.0", lifespan=lifespan)

# apps/gcs-web is deliberately served from a different origin (a separate
# static file server on its own port -- see docs/adr/0005-frontend-stack.md
# and the ?api= query param it takes) -- so every fetch() call the UI
# makes is cross-origin by design. Browsers don't apply CORS to WebSocket
# connections, which is why telemetry (delivered over /ws/telemetry and
# /ws/fleet) can keep working even when every fetch()-based command or
# mission call fails outright with a generic, unhelpful
# "TypeError: Failed to fetch" and no server-side error to show for it.
# allow_origins=["*"] is fine here: this is local dev tooling (no cookies/
# credentials are used -- every request carries its own explicit token or
# none at all), not a production deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reserved ws_manager channel key carrying every vehicle's telemetry, for
# the fleet-wide view (docs/adr/0009-multi-vehicle-fleet.md). Not a valid
# vehicle_id (vehicle_ids come from the gateway/simulator's VEHICLE_ID env
# var), so it can't collide with a real one.
FLEET_CHANNEL = "__fleet__"


def db_session():
    session = get_session()
    try:
        yield session
    finally:
        session.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _check_internal_token(x_internal_token: str | None) -> None:
    if x_internal_token != settings.internal_token:
        raise HTTPException(status_code=401, detail="invalid internal token")


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Gate for every user-facing endpoint (docs/adr/0011-api-authentication.md).
    A no-op when API_KEY isn't set (Settings.api_key == ""), which is the
    default -- Phase 1-4's zero-config local dev/testing keeps working
    unchanged unless a deployment explicitly opts into auth."""
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="missing or invalid API key")


async def require_ws_api_key(websocket: WebSocket) -> bool:
    """Same gate as require_api_key(), for the two WS routes. Browsers
    can't set custom headers on a WebSocket handshake, so the key travels
    as ?api_key= instead. Returns False (and closes the socket) on
    failure -- callers must return immediately without calling
    ws_manager.connect() in that case."""
    if not settings.api_key:
        return True
    if websocket.query_params.get("api_key") != settings.api_key:
        await websocket.close(code=1008)  # policy violation
        return False
    return True


def _error_message(exc: Exception) -> str:
    """str(exc), but never empty. Some httpx exceptions (notably timeouts
    raised internally by its transport layer) have an empty message --
    str(httpx.ReadTimeout()) == "" -- which without this falls through the
    UI's `error || detail || "unknown error"` fallback and shows up as a
    useless "unknown error" with no clue what actually happened."""
    return str(exc) or f"{type(exc).__name__} (gateway call failed with no further detail)"


@app.post("/internal/telemetry")
async def ingest_telemetry(
    sample: TelemetryIngest,
    x_internal_token: str | None = Header(default=None),
    session: Session = Depends(db_session),
) -> dict:
    _check_internal_token(x_internal_token)

    now = datetime.now(timezone.utc)
    vehicle = session.get(Vehicle, sample.vehicle_id)
    if vehicle is None:
        vehicle = Vehicle(vehicle_id=sample.vehicle_id, first_seen=now, last_seen=now)
        session.add(vehicle)
        session.flush()
        session.add(
            EventLogEntry(
                vehicle_id=sample.vehicle_id, timestamp=now, event_type="VEHICLE_CONNECTED"
            )
        )
    prev_armed = vehicle.armed
    vehicle.last_seen = now
    vehicle.armed = sample.armed
    vehicle.flight_mode = sample.flight_mode
    vehicle.current_waypoint_seq = sample.current_waypoint_seq
    if prev_armed != sample.armed:
        session.add(
            EventLogEntry(
                vehicle_id=sample.vehicle_id,
                timestamp=now,
                event_type="ARMED_CHANGED",
                detail=f"armed={sample.armed}",
            )
        )
    if not sample.armed:
        # Failsafe manager (docs/adr/0012-geofencing-failsafe.md): a
        # disarmed vehicle can't be outside its fence in any way that
        # matters, so "disarmed" is the reset condition back to NORMAL --
        # simpler and more certain than trying to detect "back inside the
        # fence" while still armed and possibly still moving. Checked on
        # every disarmed sample (not just the arm->disarm transition) so
        # a stale GEOFENCE_BREACH left over from before an API restart
        # also clears once the vehicle is next seen disarmed.
        vehicle.emergency_state = "NORMAL"

    row = TelemetrySample(timestamp=now, **sample.model_dump())
    session.add(row)

    # Failsafe manager: geofence breach -> auto-RTL. Deliberately a
    # simple, deterministic rule (not a learned/AI system -- see the ADR
    # for why that's a separate, later piece of work), and deliberately
    # debounced by emergency_state so a vehicle that stays outside the
    # fence doesn't get an RTL command re-sent on every telemetry sample.
    fence = session.get(Geofence, sample.vehicle_id)
    breached = (
        fence is not None
        and sample.armed
        and sample.lat is not None
        and sample.lon is not None
        and not point_in_polygon(sample.lat, sample.lon, fence.points)
    )
    if breached and vehicle.emergency_state != "GEOFENCE_BREACH":
        vehicle.emergency_state = "GEOFENCE_BREACH"
        session.add(
            EventLogEntry(
                vehicle_id=sample.vehicle_id,
                timestamp=now,
                event_type="GEOFENCE_BREACH",
                detail=f"lat={sample.lat}, lon={sample.lon} outside fence -- auto-RTL issued",
            )
        )
        session.commit()
        result = await _dispatch_gateway_command(sample.vehicle_id, "RTL")
        session.add(
            EventLogEntry(
                vehicle_id=sample.vehicle_id,
                event_type="FAILSAFE_RTL",
                detail=f"auto-RTL -> {result['status']}"
                + (f" ({result['error']})" if result["error"] else ""),
            )
        )

    session.commit()

    message = {
        "type": "telemetry",
        "timestamp": now.isoformat(),
        "emergency_state": vehicle.emergency_state,
        **sample.model_dump(),
    }
    await ws_manager.broadcast(sample.vehicle_id, message)
    # Phase 3: also fan out to the fleet-wide channel so the UI can show
    # every vehicle on one map without opening one socket per vehicle
    # (see docs/adr/0009-multi-vehicle-fleet.md).
    await ws_manager.broadcast(FLEET_CHANNEL, message)
    return {"status": "accepted"}


@app.get("/vehicles", response_model=list[VehicleOut], dependencies=[Depends(require_api_key)])
def list_vehicles(session: Session = Depends(db_session)):
    return session.scalars(select(Vehicle)).all()


@app.get(
    "/vehicles/{vehicle_id}", response_model=VehicleOut, dependencies=[Depends(require_api_key)]
)
def get_vehicle(vehicle_id: str, session: Session = Depends(db_session)):
    vehicle = session.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="vehicle not found")
    return vehicle


@app.get(
    "/vehicles/{vehicle_id}/telemetry",
    response_model=list[TelemetryOut],
    dependencies=[Depends(require_api_key)],
)
def get_telemetry(
    vehicle_id: str, limit: int = 100, session: Session = Depends(db_session)
):
    limit = max(1, min(limit, 1000))
    stmt = (
        select(TelemetrySample)
        .where(TelemetrySample.vehicle_id == vehicle_id)
        .order_by(TelemetrySample.timestamp.desc())
        .limit(limit)
    )
    rows = session.scalars(stmt).all()
    return list(reversed(rows))


def _gateway_url_for(vehicle_id: str) -> str:
    """Which gateway a vehicle's commands/missions go to. Phase 3
    (docs/adr/0009-multi-vehicle-fleet.md): each vehicle has its own
    gateway process; GATEWAY_URLS_JSON maps vehicle_id -> base URL, and
    anything not in that map falls back to gateway_base_url (correct for
    Phase 1/2's single-vehicle setup with zero config changes needed)."""
    return settings.gateway_urls.get(vehicle_id, settings.gateway_base_url)


async def _dispatch_gateway_command(
    vehicle_id: str, command_type: str, altitude_m: float | None = None
) -> dict:
    """POST /command to vehicle_id's gateway and normalize the outcome
    into {"status": "ACKED"|"FAILED", "mav_result": int|None, "error": str|None}.
    Shared by create_command() and start_mission() so both go through one
    place for gateway-call error handling."""
    try:
        async with httpx.AsyncClient(timeout=settings.command_timeout_s) as client:
            resp = await client.post(
                f"{_gateway_url_for(vehicle_id)}/command",
                json={"type": command_type, "altitude_m": altitude_m},
            )
        if resp.status_code == 200:
            body = resp.json()
            acked = bool(body.get("acked"))
            return {
                "status": "ACKED" if acked else "FAILED",
                "mav_result": body.get("mav_result"),
                # A NACK (gateway responded 200, but the vehicle didn't
                # acknowledge the command -- e.g. rejected because it's
                # mid-RTL or another command is in flight) previously left
                # error=None here, which the UI's `error || detail ||
                # "unknown error"` fallback then showed as a bare, useless
                # "unknown error" -- indistinguishable from a real crash,
                # same failure mode _error_message() below already fixed
                # for the exception path, just not for this one.
                "error": None if acked else f"command not acknowledged (mav_result={body.get('mav_result')})",
            }
        return {
            "status": "FAILED",
            "mav_result": None,
            "error": f"gateway returned {resp.status_code}: {resp.text}",
        }
    except httpx.HTTPError as exc:
        return {"status": "FAILED", "mav_result": None, "error": _error_message(exc)}


@app.post(
    "/vehicles/{vehicle_id}/commands",
    response_model=CommandOut,
    dependencies=[Depends(require_api_key)],
)
async def create_command(
    vehicle_id: str, req: CommandCreate, session: Session = Depends(db_session)
):
    command = Command(
        vehicle_id=vehicle_id,
        command_type=req.type,
        altitude_m=req.altitude_m,
        status="PENDING",
    )
    session.add(command)
    session.commit()
    session.refresh(command)

    result = await _dispatch_gateway_command(vehicle_id, req.type, req.altitude_m)
    command.status = result["status"]
    command.mav_result = result["mav_result"]
    command.error = result["error"]

    command.resolved_at = datetime.now(timezone.utc)
    session.add(
        EventLogEntry(
            vehicle_id=vehicle_id,
            event_type="COMMAND",
            detail=f"{req.type} -> {command.status}",
        )
    )
    session.commit()
    session.refresh(command)
    return command


@app.get(
    "/vehicles/{vehicle_id}/commands/{command_id}",
    response_model=CommandOut,
    dependencies=[Depends(require_api_key)],
)
def get_command(vehicle_id: str, command_id: str, session: Session = Depends(db_session)):
    command = session.get(Command, command_id)
    if command is None or command.vehicle_id != vehicle_id:
        raise HTTPException(status_code=404, detail="command not found")
    return command


@app.post(
    "/vehicles/{vehicle_id}/missions",
    response_model=MissionOut,
    dependencies=[Depends(require_api_key)],
)
async def create_mission(
    vehicle_id: str, req: MissionCreate, session: Session = Depends(db_session)
):
    waypoints = [wp.model_dump() for wp in req.waypoints]

    # Geofence check (docs/adr/0012-geofencing-failsafe.md): reject a
    # mission outright if the vehicle has an active fence and any
    # waypoint falls outside it, rather than letting it upload and only
    # catching the problem once the vehicle is already flying toward a
    # forbidden point and the failsafe manager has to auto-RTL it.
    fence = session.get(Geofence, vehicle_id)
    if fence is not None:
        bad = waypoints_outside_fence(waypoints, fence.points)
        if bad:
            raise HTTPException(
                status_code=422,
                detail=f"waypoint(s) {bad} fall outside the vehicle's geofence",
            )

    mission = Mission(vehicle_id=vehicle_id, waypoints=waypoints, status="PENDING")
    session.add(mission)
    session.commit()
    session.refresh(mission)

    try:
        async with httpx.AsyncClient(timeout=settings.mission_upload_timeout_s) as client:
            resp = await client.post(
                f"{_gateway_url_for(vehicle_id)}/mission", json={"waypoints": waypoints}
            )
        if resp.status_code == 200 and resp.json().get("accepted"):
            mission.status = "UPLOADED"
        else:
            mission.status = "FAILED"
            mission.error = f"gateway returned {resp.status_code}: {resp.text}"
    except httpx.HTTPError as exc:
        mission.status = "FAILED"
        mission.error = _error_message(exc)

    session.add(
        EventLogEntry(
            vehicle_id=vehicle_id,
            event_type="MISSION_UPLOAD",
            detail=f"{len(waypoints)} waypoints -> {mission.status}",
        )
    )
    session.commit()
    session.refresh(mission)
    return mission


@app.get(
    "/vehicles/{vehicle_id}/missions",
    response_model=list[MissionOut],
    dependencies=[Depends(require_api_key)],
)
def list_missions(vehicle_id: str, session: Session = Depends(db_session)):
    stmt = (
        select(Mission)
        .where(Mission.vehicle_id == vehicle_id)
        .order_by(Mission.created_at)
    )
    return session.scalars(stmt).all()


@app.get(
    "/vehicles/{vehicle_id}/missions/{mission_id}",
    response_model=MissionOut,
    dependencies=[Depends(require_api_key)],
)
def get_mission(vehicle_id: str, mission_id: str, session: Session = Depends(db_session)):
    mission = session.get(Mission, mission_id)
    if mission is None or mission.vehicle_id != vehicle_id:
        raise HTTPException(status_code=404, detail="mission not found")
    return mission


@app.post(
    "/vehicles/{vehicle_id}/missions/{mission_id}/start",
    response_model=MissionOut,
    dependencies=[Depends(require_api_key)],
)
async def start_mission(
    vehicle_id: str, mission_id: str, session: Session = Depends(db_session)
):
    mission = session.get(Mission, mission_id)
    if mission is None or mission.vehicle_id != vehicle_id:
        raise HTTPException(status_code=404, detail="mission not found")
    if mission.status != "UPLOADED":
        raise HTTPException(
            status_code=400,
            detail=f"mission must be UPLOADED to start (currently {mission.status})",
        )

    result = await _dispatch_gateway_command(vehicle_id, "MISSION_START")
    if result["status"] == "ACKED":
        mission.status = "ACTIVE"
        mission.started_at = datetime.now(timezone.utc)
        vehicle = session.get(Vehicle, vehicle_id)
        if vehicle is not None:
            vehicle.active_mission_id = mission.mission_id
    else:
        mission.status = "FAILED"
        mission.error = result["error"] or f"MISSION_START not acked (mav_result={result['mav_result']})"

    session.add(
        EventLogEntry(
            vehicle_id=vehicle_id,
            event_type="MISSION_START",
            detail=f"{mission_id} -> {mission.status}",
        )
    )
    session.commit()
    session.refresh(mission)
    return mission


@app.post(
    "/vehicles/{vehicle_id}/geofence",
    response_model=GeofenceOut,
    dependencies=[Depends(require_api_key)],
)
def set_geofence(vehicle_id: str, req: GeofenceCreate, session: Session = Depends(db_session)):
    """Define/replace vehicle_id's geofence -- see
    docs/adr/0012-geofencing-failsafe.md. A vehicle has at most one active
    fence; posting a new one replaces the old one outright (same pattern
    as re-uploading a mission)."""
    points = [p.model_dump() for p in req.points]
    fence = session.get(Geofence, vehicle_id)
    if fence is None:
        fence = Geofence(vehicle_id=vehicle_id, points=points)
        session.add(fence)
    else:
        fence.points = points
    session.add(
        EventLogEntry(
            vehicle_id=vehicle_id,
            event_type="GEOFENCE_SET",
            detail=f"{len(points)} points",
        )
    )
    session.commit()
    session.refresh(fence)
    return fence


@app.get(
    "/vehicles/{vehicle_id}/geofence",
    response_model=GeofenceOut,
    dependencies=[Depends(require_api_key)],
)
def get_geofence(vehicle_id: str, session: Session = Depends(db_session)):
    fence = session.get(Geofence, vehicle_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="no geofence set for this vehicle")
    return fence


@app.delete(
    "/vehicles/{vehicle_id}/geofence",
    dependencies=[Depends(require_api_key)],
)
def delete_geofence(vehicle_id: str, session: Session = Depends(db_session)):
    fence = session.get(Geofence, vehicle_id)
    if fence is None:
        raise HTTPException(status_code=404, detail="no geofence set for this vehicle")
    session.delete(fence)
    session.add(EventLogEntry(vehicle_id=vehicle_id, event_type="GEOFENCE_CLEARED"))
    session.commit()
    return {"status": "cleared"}


@app.websocket("/ws/telemetry/{vehicle_id}")
async def telemetry_ws(websocket: WebSocket, vehicle_id: str):
    if not await require_ws_api_key(websocket):
        return
    await ws_manager.connect(vehicle_id, websocket)
    try:
        while True:
            # We don't expect inbound messages; this just detects disconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(vehicle_id, websocket)


@app.websocket("/ws/fleet")
async def fleet_ws(websocket: WebSocket):
    """Every vehicle's telemetry over one connection -- see
    docs/adr/0009-multi-vehicle-fleet.md. Same connect/disconnect pattern
    as /ws/telemetry/{vehicle_id}, just on the reserved fleet channel."""
    if not await require_ws_api_key(websocket):
        return
    await ws_manager.connect(FLEET_CHANNEL, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(FLEET_CHANNEL, websocket)
