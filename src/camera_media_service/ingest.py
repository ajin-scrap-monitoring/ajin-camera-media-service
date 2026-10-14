from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from camera_media_service.pipeline import FrameValidator, LiveFrameHub
from camera_media_service.registry import ConnectionRegistry
from camera_media_service.store import LatestFrameStore
from camera_media_service.telemetry import IngestTelemetryTracker


class WebSocketLike(Protocol):
    def __aiter__(self) -> WebSocketLike: ...

    async def __anext__(self) -> object: ...

    async def close(self, code: int, reason: str) -> None: ...


@dataclass(slots=True)
class IngestProtocolError(Exception):
    close_code: int
    reason: str


class TextFrameRejectedError(IngestProtocolError):
    def __init__(self) -> None:
        super().__init__(1003, "binary frames required")


class InvalidJpegFrameError(IngestProtocolError):
    def __init__(self) -> None:
        super().__init__(1007, "invalid jpeg frame")


class OversizedFrameError(IngestProtocolError):
    def __init__(self) -> None:
        super().__init__(1009, "frame too large")


@dataclass(slots=True)
class IngestService:
    registry: ConnectionRegistry
    frame_store: LatestFrameStore
    max_frame_bytes: int
    validator: FrameValidator | None = None
    hub: LiveFrameHub | None = None
    telemetry: IngestTelemetryTracker | None = None

    def __post_init__(self) -> None:
        if self.validator is None:
            self.validator = FrameValidator(max_frame_bytes=self.max_frame_bytes)
        if self.hub is None:
            self.hub = LiveFrameHub(frame_store=self.frame_store)

    async def receive(self, camera_id: str, connection_id: str, websocket: WebSocketLike) -> None:
        if self.telemetry is not None:
            self.telemetry.record_connection_start()

        displaced = self.registry.claim_connection(camera_id, connection_id, websocket)
        if self.hub.connection_observer is not None:
            self.hub.connection_observer(camera_id, True, time.time())
        try:
            if displaced is not None:
                try:
                    await displaced.close(1008, "replaced by newer connection")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

            async for message in websocket:
                if isinstance(message, str):
                    if self.telemetry is not None:
                        self.telemetry.record_frame_rejected()
                    error = TextFrameRejectedError()
                    await websocket.close(error.close_code, error.reason)
                    raise error

                frame = memoryview(message).tobytes()
                assert self.validator is not None
                if not self.validator.is_within_size(frame):
                    if self.telemetry is not None:
                        self.telemetry.record_frame_rejected()
                    error = OversizedFrameError()
                    await websocket.close(error.close_code, error.reason)
                    raise error

                if not self.validator.is_jpeg(frame):
                    if self.telemetry is not None:
                        self.telemetry.record_frame_rejected()
                    error = InvalidJpegFrameError()
                    await websocket.close(error.close_code, error.reason)
                    raise error

                if not self.registry.is_current(camera_id, connection_id):
                    return

                assert self.hub is not None
                self.hub.publish(camera_id, frame)
                if self.telemetry is not None:
                    self.telemetry.record_frame_ingested(len(frame))
        finally:
            if self.telemetry is not None:
                self.telemetry.record_connection_end()
            if (
                self.registry.is_current(camera_id, connection_id)
                and self.hub.connection_observer is not None
            ):
                self.hub.connection_observer(camera_id, False, time.time())
            self.registry.release(camera_id, connection_id)
