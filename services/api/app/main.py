from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session, init_db
from .models import Command, EventLogEntry, TelemetrySample, Vehicle
from .schemas import CommandCreate, CommandOut, TelemetryIngest, TelemetryOut, VehicleOut
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

    try:
        async with httpx.AsyncClient(timeout=settings.command_timeout_s) as client:
            resp = await client.post(
                f"{settings.gateway_base_url}/command",
                json={"type": req.type, "altitude_m": req.altitude_m},
            )
        if resp.status_code == 200:
            body = resp.json()
            command.status = "ACKED" if body.get("acked") else "FAILED"
            command.mav_result = body.get("mav_result")
        else:
            command.status = "FAILED"
            command.error = f"gateway returned {resp.status_code}: {resp.text}"
    except httpx.HTTPError as exc:
        command.status = "FAILED"
        command.error = str(exc)

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
