from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .commands import (
    SUPPORTED_COMMAND_TYPES,
    UnsupportedCommand,
    build_command,
    mission_upload_timeout_for,
)
from .config import settings
from .mavlink_client import (
    CommandTimeout,
    MissionUploadRejected,
    MissionUploadTimeout,
    gateway_client,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    gateway_client.start()
    print(
        f"[gateway] listening for MAVLink on "
        f"{settings.mavlink_bind_host}:{settings.mavlink_bind_port}, "
        f"posting telemetry to {settings.api_base_url}"
    )
    yield
    gateway_client.stop()


app = FastAPI(title="UAS-SYSTEM Telemetry Gateway", version="0.1.0", lifespan=lifespan)


class CommandRequest(BaseModel):
    type: str
    altitude_m: float | None = None


class WaypointRequest(BaseModel):
    lat: float
    lon: float
    alt_m: float


class MissionUploadRequest(BaseModel):
    waypoints: list[WaypointRequest]


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "vehicle_id": settings.vehicle_id,
        "mavlink_connected": gateway_client.is_connected(),
    }


@app.post("/command")
def send_command(req: CommandRequest) -> dict:
    if req.type not in SUPPORTED_COMMAND_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported command type: {req.type}")
    try:
        mav_cmd = build_command(req.type, req.altitude_m)
    except UnsupportedCommand as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not gateway_client.is_connected():
        raise HTTPException(status_code=503, detail="no vehicle connected (no HEARTBEAT yet)")

    try:
        result = gateway_client.send_command(mav_cmd.command_id, mav_cmd.params)
    except CommandTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc))

    return {"mav_result": result, "acked": result == 0}


@app.post("/mission")
def upload_mission(req: MissionUploadRequest) -> dict:
    if not gateway_client.is_connected():
        raise HTTPException(status_code=503, detail="no vehicle connected (no HEARTBEAT yet)")
    if not req.waypoints:
        raise HTTPException(status_code=400, detail="waypoints must be non-empty")

    waypoints = [wp.model_dump() for wp in req.waypoints]
    try:
        gateway_client.upload_mission(waypoints, timeout=mission_upload_timeout_for(len(waypoints)))
    except MissionUploadTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except MissionUploadRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"accepted": True, "count": len(waypoints)}
