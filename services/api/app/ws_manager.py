"""In-process WebSocket fan-out. See docs/adr/0006-internal-messaging.md for
why this is in-process rather than a real pub/sub layer in Phase 1."""
from collections import defaultdict

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, vehicle_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[vehicle_id].add(ws)

    def disconnect(self, vehicle_id: str, ws: WebSocket) -> None:
        self._connections[vehicle_id].discard(ws)

    async def broadcast(self, vehicle_id: str, message: dict) -> None:
        dead = []
        for ws in list(self._connections.get(vehicle_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(vehicle_id, ws)


ws_manager = ConnectionManager()
