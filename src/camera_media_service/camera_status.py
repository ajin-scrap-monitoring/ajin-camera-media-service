"""Current connection observations; no state is restored from recordings or disk."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from camera_media_service.recording import ApiError, iso


@dataclass(slots=True)
class _Observation:
    connection_id: str
    connected_at: float
    connected: bool = True
    last_frame_at: float | None = None
    last_frame_received_at: float | None = None
    disconnect_reason: str = "CONNECTION_CLOSED"


class CameraStatusTracker:
    """Assess freshness using elapsed time, with UTC receipt times for display only."""

    def __init__(
        self,
        cameras: Iterable[str],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        freshness_seconds: float = 5,
    ) -> None:
        if not 0 < freshness_seconds <= 5:
            raise ValueError("freshness_seconds must be positive and at most 5")
        self.cameras = frozenset(cameras)
        self.clock, self.monotonic = clock, monotonic
        self.freshness_seconds = freshness_seconds
        self._observations: dict[str, _Observation] = {}

    def connected(self, camera_id: str, connection_id: str) -> None:
        self._camera(camera_id)
        self._observations[camera_id] = _Observation(connection_id, self.monotonic())

    def frame(self, camera_id: str, connection_id: str, received_at: float) -> None:
        self._camera(camera_id)
        observation = self._observations.get(camera_id)
        if (
            observation is None
            or observation.connection_id != connection_id
            or not observation.connected
        ):
            return
        observation.last_frame_at = self.monotonic()
        observation.last_frame_received_at = received_at

    def disconnected(self, camera_id: str, connection_id: str, reason_code: str) -> None:
        self._camera(camera_id)
        observation = self._observations.get(camera_id)
        if (
            observation is None
            or observation.connection_id != connection_id
            or not observation.connected
        ):
            return
        observation.connected = False
        observation.disconnect_reason = reason_code

    def snapshot(self, camera_id: str) -> dict:
        self._camera(camera_id)
        wall, steady = self.clock(), self.monotonic()
        observation = self._observations.get(camera_id)
        status, connection_state, reason = "unknown", "unknown", "NO_OBSERVATION"
        last_received = None
        valid_for = self.freshness_seconds
        if observation is not None:
            last_received = observation.last_frame_received_at
            if not observation.connected:
                status, connection_state = "interrupted", "disconnected"
                reason = observation.disconnect_reason
            else:
                connection_state = "connected"
                has_frame = observation.last_frame_at is not None
                reference = observation.last_frame_at if has_frame else observation.connected_at
                remaining = self.freshness_seconds - max(0, steady - reference)
                if remaining <= 0:
                    status = "interrupted"
                    reason = "FRAME_STALE" if has_frame else "FRAME_TIMEOUT"
                else:
                    status = "healthy" if has_frame else "unknown"
                    reason = "FRAME_RECEIVED" if has_frame else "AWAITING_FRAME"
                    valid_for = min(valid_for, remaining)

        return {
            "camera_id": camera_id,
            "status_basis": "accepted_jpeg_receipt",
            "status": status,
            "connection_state": connection_state,
            "last_frame_received_at": iso(last_received) if last_received is not None else None,
            "assessed_at": iso(wall),
            # Project remaining monotonic lifetime onto the current wall clock. Using
            # receipt_wall + freshness here would extend caches after a backward jump.
            "valid_until_at": iso(wall + valid_for),
            "reason_code": reason,
            "freshness_seconds": self.freshness_seconds,
        }

    def _camera(self, camera_id: str) -> None:
        if camera_id not in self.cameras:
            raise ApiError(404, "CAMERA_NOT_FOUND", "Camera is not registered.")
