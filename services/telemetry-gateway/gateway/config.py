import os
from dataclasses import dataclass


@dataclass
class Settings:
    vehicle_id: str = os.environ.get("VEHICLE_ID", "sim-1")
    mavlink_bind_host: str = os.environ.get("GATEWAY_MAVLINK_HOST", "0.0.0.0")
    mavlink_bind_port: int = int(os.environ.get("GATEWAY_MAVLINK_PORT", "14550"))
    api_base_url: str = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
    internal_token: str = os.environ.get("UAS_INTERNAL_TOKEN", "dev-secret")
    telemetry_post_interval_s: float = float(os.environ.get("TELEMETRY_POST_INTERVAL_S", "0.5"))
    host: str = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port: int = int(os.environ.get("GATEWAY_PORT", "8001"))


settings = Settings()
