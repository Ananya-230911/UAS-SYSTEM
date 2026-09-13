import os
from dataclasses import dataclass


@dataclass
class Settings:
    # SQLite is the Phase 1 substitution for TimescaleDB -- see
    # docs/adr/0004-telemetry-storage.md.
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./uas.db")
    internal_token: str = os.environ.get("UAS_INTERNAL_TOKEN", "dev-secret")
    gateway_base_url: str = os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8001")
    command_timeout_s: float = float(os.environ.get("COMMAND_TIMEOUT_S", "8"))
    # Deliberately longer than command_timeout_s: a mission upload is a
    # multi-round-trip handshake (one MISSION_REQUEST_INT/MISSION_ITEM_INT
    # pair per waypoint, see docs/adr/0008-mission-protocol.md), not a
    # single ack like ARM/TAKEOFF/RTL. This must stay comfortably above the
    # gateway's own internal upload_mission() timeout (see
    # gateway/commands.py's mission_upload_timeout_for()) or the API gives
    # up before the gateway does, on a mission that would have succeeded.
    mission_upload_timeout_s: float = float(os.environ.get("MISSION_UPLOAD_TIMEOUT_S", "30"))
    host: str = os.environ.get("API_HOST", "0.0.0.0")
    port: int = int(os.environ.get("API_PORT", "8000"))


settings = Settings()
