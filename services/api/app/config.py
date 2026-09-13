import json
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # SQLite is the Phase 1 substitution for TimescaleDB -- see
    # docs/adr/0004-telemetry-storage.md.
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./uas.db")
    internal_token: str = os.environ.get("UAS_INTERNAL_TOKEN", "dev-secret")
    # Default/fallback gateway -- correct for Phase 1/2's single vehicle.
    gateway_base_url: str = os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8001")
    # Phase 3 (docs/adr/0009-multi-vehicle-fleet.md): each vehicle has its
    # own gateway process, so a command/mission for vehicle X must go to
    # X's gateway, not whichever one gateway_base_url happens to point at.
    # GATEWAY_URLS_JSON is a {"vehicle_id": "http://host:port"} map; a
    # vehicle_id missing from it falls back to gateway_base_url, which
    # keeps Phase 1/2's single-gateway setup working with zero config
    # changes. See _gateway_url_for() in app/main.py.
    gateway_urls: dict = field(
        default_factory=lambda: json.loads(os.environ.get("GATEWAY_URLS_JSON", "{}"))
    )
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
