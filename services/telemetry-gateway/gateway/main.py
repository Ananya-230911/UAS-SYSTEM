from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .commands import SUPPORTED_COMMAND_TYPES, UnsupportedCommand, build_command
from .config import settings
from .mavlink_client import CommandTimeout, gateway_client


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
