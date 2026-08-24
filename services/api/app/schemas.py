from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TelemetryIngest(BaseModel):
    vehicle_id: str
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    relative_alt_m: float | None = None
    heading_deg: float | None = None
    groundspeed_ms: float | None = None
    roll: float | None = None
    pitch: float | None = None
    yaw: float | None = None
    battery_pct: float | None = None
    battery_voltage_mv: float | None = None
    gps_fix_type: int | None = None
    satellites_visible: int | None = None
    armed: bool = False
    flight_mode: str = "UNKNOWN"


class TelemetryOut(TelemetryIngest):
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


class VehicleOut(BaseModel):
    vehicle_id: str
    first_seen: datetime
    last_seen: datetime
    armed: bool
    flight_mode: str

    model_config = ConfigDict(from_attributes=True)


class CommandCreate(BaseModel):
    type: str
    altitude_m: float | None = None


class CommandOut(BaseModel):
    command_id: str
    vehicle_id: str
    command_type: str
    altitude_m: float | None = None
    status: str
    mav_result: int | None = None
    error: str | None = None
    issued_at: datetime
    resolved_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
