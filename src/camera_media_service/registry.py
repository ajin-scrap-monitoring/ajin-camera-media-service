from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ClosableConnection(Protocol):
    async def close(self, code: int, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _Connection:
    connection_id: str
    websocket: ClosableConnection | None


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, _Connection] = {}

    def claim(self, camera_id: str, connection_id: str) -> str | None:
        displaced = self._claim(camera_id, connection_id, None)
        return None if displaced is None else displaced.connection_id

    def claim_connection(
        self,
        camera_id: str,
        connection_id: str,
        websocket: ClosableConnection,
    ) -> ClosableConnection | None:
        displaced = self._claim(camera_id, connection_id, websocket)
        return None if displaced is None else displaced.websocket

    def _claim(
        self,
        camera_id: str,
        connection_id: str,
        websocket: ClosableConnection | None,
    ) -> _Connection | None:
        displaced = self._connections.get(camera_id)
        if displaced is not None and displaced.connection_id == connection_id:
            return None

        self._connections[camera_id] = _Connection(connection_id, websocket)
        return displaced

    def release(self, camera_id: str, connection_id: str) -> bool:
        current = self._connections.get(camera_id)
        if current is None or current.connection_id != connection_id:
            return False

        del self._connections[camera_id]
        return True

    def is_current(self, camera_id: str, connection_id: str) -> bool:
        current = self._connections.get(camera_id)
        return current is not None and current.connection_id == connection_id
