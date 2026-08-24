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
    host: str = os.environ.get("API_HOST", "0.0.0.0")
    port: int = int(os.environ.get("API_PORT", "8000"))


settings = Settings()
