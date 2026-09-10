from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session, init_db
from .models import Command, EventLogEntry, Mission, TelemetrySample, Vehicle
from .schemas import (
    CommandCreate,
    CommandOut,
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

    row = TelemetrySample(timestamp=now, **sample.model_dump())
    session.add(row)
    session.commit()

    await ws_manager.broadcast(
        sample.vehicle_id, {"type": "telemetry", "timestamp": now.isoformat(), **sample.model_dump()}
    )
    return {"status": "accepted"}


@app.get("/vehicles", response_model=list[VehicleOut])
def list_vehicles(session: Session = Depends(db_session)):
    return session.scalars(select(Vehicle)).all()


@app.get("/vehicles/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(vehicle_id: str, session: Session = Depends(db_session)):
    vehicle = session.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="vehicle not found")
    return vehicle


@app.get("/vehicles/{vehicle_id}/telemetry", response_model=list[TelemetryOut])
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


async def _dispatch_gateway_command(command_type: str, altitude_m: float | None = None) -> dict:
    """POST /command to the gateway and normalize the outcome into
    {"status": "ACKED"|"FAILED", "mav_result": int|None, "error": str|None}.
    Shared by create_command() and start_mission() so both go through one
    place for gateway-call error handling."""
    try:
        async with httpx.AsyncClient(timeout=settings.command_timeout_s) as client:
            resp = await client.post(
                f"{settings.gateway_base_url}/command",
                json={"type": command_type, "altitude_m": altitude_m},
            )
        if resp.status_code == 200:
            body = resp.json()
            return {
                "status": "ACKED" if body.get("acked") else "FAILED",
                "mav_result": body.get("mav_result"),
                "error": None,
            }
        return {
            "status": "FAILED",
            "mav_result": None,
            "error": f"gateway returned {resp.status_code}: {resp.text}",
        }
    except httpx.HTTPError as exc:
        return {"status": "FAILED", "mav_result": None, "error": str(exc)}


@app.post("/vehicles/{vehicle_id}/commands", response_model=CommandOut)
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

    result = await _dispatch_gateway_command(req.type, req.altitude_m)
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


@app.get("/vehicles/{vehicle_id}/commands/{command_id}", response_model=CommandOut)
def get_command(vehicle_id: str, command_id: str, session: Session = Depends(db_session)):
    command = session.get(Command, command_id)
    if command is None or command.vehicle_id != vehicle_id:
        raise HTTPException(status_code=404, detail="command not found")
    return command


@app.post("/vehicles/{vehicle_id}/missions", response_model=MissionOut)
async def create_mission(
    vehicle_id: str, req: MissionCreate, session: Session = Depends(db_session)
):
    waypoints = [wp.model_dump() for wp in req.waypoints]
    mission = Mission(vehicle_id=vehicle_id, waypoints=waypoints, status="PENDING")
    session.add(mission)
    session.commit()
    session.refresh(mission)

    try:
        async with httpx.AsyncClient(timeout=settings.command_timeout_s) as client:
            resp = await client.post(
                f"{settings.gateway_base_url}/mission", json={"waypoints": waypoints}
            )
        if resp.status_code == 200 and resp.json().get("accepted"):
            mission.status = "UPLOADED"
        else:
            mission.status = "FAILED"
            mission.error = f"gateway returned {resp.status_code}: {resp.text}"
    except httpx.HTTPError as exc:
        mission.status = "FAILED"
        mission.error = str(exc)

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


@app.get("/vehicles/{vehicle_id}/missions", response_model=list[MissionOut])
def list_missions(vehicle_id: str, session: Session = Depends(db_session)):
    stmt = (
        select(Mission)
        .where(Mission.vehicle_id == vehicle_id)
        .order_by(Mission.created_at)
    )
    return session.scalars(stmt).all()


@app.get("/vehicles/{vehicle_id}/missions/{mission_id}", response_model=MissionOut)
def get_mission(vehicle_id: str, mission_id: str, session: Session = Depends(db_session)):
    mission = session.get(Mission, mission_id)
    if mission is None or mission.vehicle_id != vehicle_id:
        raise HTTPException(status_code=404, detail="mission not found")
    return mission


@app.post("/vehicles/{vehicle_id}/missions/{mission_id}/start", response_model=MissionOut)
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

    result = await _dispatch_gateway_command("MISSION_START")
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


@app.websocket("/ws/telemetry/{vehicle_id}")
async def telemetry_ws(websocket: WebSocket, vehicle_id: str):
    await ws_manager.connect(vehicle_id, websocket)
    try:
        while True:
            # We don't expect inbound messages; this just detects disconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(vehicle_id, websocket)
