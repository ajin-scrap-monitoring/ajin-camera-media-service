from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from camera_media_service import camera_status
from camera_media_service.ingest import (
    IngestService,
    InvalidJpegFrameError,
    OversizedFrameError,
    TextFrameRejectedError,
)
from camera_media_service.recording import ApiError
from camera_media_service.registry import ConnectionRegistry
from camera_media_service.store import LatestFrameStore


class Clock:
    wall = 1_700_000_000.0
    steady = 100.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.steady += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tracker(clock):
    return camera_status.CameraStatusTracker(
        {"cam-1", "cam-2"}, clock=lambda: clock.wall, monotonic=lambda: clock.steady
    )


def test_no_observations_and_restart_are_unknown(tracker, clock):
    status = tracker.snapshot("cam-1")
    assert status == {
        "camera_id": "cam-1",
        "status": "unknown",
        "status_basis": "accepted_jpeg_receipt",
        "connection_state": "unknown",
        "last_frame_received_at": None,
        "assessed_at": "2023-11-14T22:13:20.000Z",
        "valid_until_at": "2023-11-14T22:13:25.000Z",
        "reason_code": "NO_OBSERVATION",
        "freshness_seconds": 5,
    }
    tracker.connected("cam-1", "old")
    tracker.frame("cam-1", "old", clock.wall)
    restarted = camera_status.CameraStatusTracker({"cam-1"})
    assert restarted.snapshot("cam-1")["status"] == "unknown"


def test_unknown_camera_is_not_found(tracker):
    with pytest.raises(ApiError) as exc:
        tracker.snapshot("unregistered")
    assert (exc.value.status, exc.value.code) == (404, "CAMERA_NOT_FOUND")


def test_connection_requires_first_frame_then_times_out_at_boundary(tracker, clock):
    tracker.connected("cam-1", "one")
    clock.advance(4.9)
    status = tracker.snapshot("cam-1")
    assert (status["status"], status["reason_code"]) == ("unknown", "AWAITING_FRAME")
    assert status["connection_state"] == "connected"
    assert status["valid_until_at"] == "2023-11-14T22:13:25.000Z"
    clock.advance(0.1)
    status = tracker.snapshot("cam-1")
    assert (status["status"], status["reason_code"]) == ("interrupted", "FRAME_TIMEOUT")


def test_current_frame_is_healthy_only_until_freshness_boundary(tracker, clock):
    tracker.connected("cam-1", "one")
    tracker.frame("cam-1", "one", clock.wall)
    clock.advance(4)
    status = tracker.snapshot("cam-1")
    assert (status["status"], status["reason_code"]) == ("healthy", "FRAME_RECEIVED")
    assert status["last_frame_received_at"] == "2023-11-14T22:13:20.000Z"
    assert status["valid_until_at"] == "2023-11-14T22:13:25.000Z"
    clock.advance(1)
    status = tracker.snapshot("cam-1")
    assert (status["status"], status["reason_code"]) == ("interrupted", "FRAME_STALE")
    tracker.frame("cam-1", "one", clock.wall)
    assert tracker.snapshot("cam-1")["status"] == "healthy"


def test_backward_wall_jump_does_not_extend_freshness_or_cache_validity(tracker, clock):
    tracker.connected("cam-1", "one")
    tracker.frame("cam-1", "one", clock.wall)
    clock.advance(4)
    clock.wall -= 3_600
    status = tracker.snapshot("cam-1")
    assessed = datetime.fromisoformat(status["assessed_at"])
    valid_until = datetime.fromisoformat(status["valid_until_at"])
    assert (valid_until - assessed).total_seconds() == 1
    clock.advance(1)
    assert tracker.snapshot("cam-1")["reason_code"] == "FRAME_STALE"


def test_forward_wall_jump_does_not_age_current_frame(tracker, clock):
    tracker.connected("cam-1", "one")
    tracker.frame("cam-1", "one", clock.wall)
    clock.wall += 3_600
    assert tracker.snapshot("cam-1")["status"] == "healthy"


def test_reconnect_ignores_previous_connection_frames_and_disconnects(tracker, clock):
    tracker.connected("cam-1", "old")
    tracker.frame("cam-1", "old", clock.wall)
    tracker.connected("cam-1", "new")
    tracker.frame("cam-1", "old", clock.wall)
    tracker.disconnected("cam-1", "old", "CONNECTION_CLOSED")
    status = tracker.snapshot("cam-1")
    assert (status["status"], status["reason_code"]) == ("unknown", "AWAITING_FRAME")
    assert status["last_frame_received_at"] is None
    assert status["connection_state"] == "connected"
    tracker.frame("cam-1", "new", clock.wall)
    tracker.disconnected("cam-1", "old", "CONNECTION_CLOSED")
    assert tracker.snapshot("cam-1")["status"] == "healthy"


def test_disconnected_is_interrupted_even_with_fresh_frame_and_ignores_late_frame(tracker, clock):
    tracker.connected("cam-1", "one")
    tracker.frame("cam-1", "one", clock.wall)
    tracker.disconnected("cam-1", "one", "CONNECTION_CLOSED")
    tracker.frame("cam-1", "one", clock.wall)
    status = tracker.snapshot("cam-1")
    assert (status["status"], status["reason_code"]) == ("interrupted", "CONNECTION_CLOSED")
    assert status["connection_state"] == "disconnected"


def test_cameras_are_independent(tracker, clock):
    tracker.connected("cam-1", "one")
    tracker.frame("cam-1", "one", clock.wall)
    assert tracker.snapshot("cam-1")["status"] == "healthy"
    assert tracker.snapshot("cam-2")["reason_code"] == "NO_OBSERVATION"


class QueueSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.waiting = asyncio.Event()
        self.closes = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.waiting.set()
        value = await self.messages.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def close(self, code, reason):
        self.closes.append((code, reason))


@pytest.fixture
def service(tracker):
    return IngestService(ConnectionRegistry(), LatestFrameStore(), 1024, status_tracker=tracker)


@pytest.mark.asyncio
async def test_ingest_current_valid_frame_updates_receipt_and_cancellation_cleans_up(
    service, tracker, monkeypatch
):
    monkeypatch.setattr("camera_media_service.pipeline.time.time", lambda: 1_700_000_001.234)
    socket = QueueSocket()
    task = asyncio.create_task(service.receive("cam-1", "one", socket))
    await socket.waiting.wait()
    assert tracker.snapshot("cam-1")["reason_code"] == "AWAITING_FRAME"
    socket.waiting.clear()
    socket.messages.put_nowait(b"\xff\xd8valid\xff\xd9")
    await socket.waiting.wait()
    status = tracker.snapshot("cam-1")
    assert status["status"] == "healthy"
    assert status["last_frame_received_at"] == "2023-11-14T22:13:21.234Z"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = tracker.snapshot("cam-1")
    assert status["connection_state"] == "disconnected"
    assert status["reason_code"] == "CANCELLED"
    assert not service.registry.is_current("cam-1", "one")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "error", "reason"),
    [
        ("text", TextFrameRejectedError, "TEXT_FRAME_REJECTED"),
        (b"bad jpeg", InvalidJpegFrameError, "INVALID_JPEG"),
        (b"\xff\xd8" + b"a" * 1024 + b"\xff\xd9", OversizedFrameError, "FRAME_TOO_LARGE"),
    ],
)
async def test_ingest_protocol_rejection_never_provides_healthy_evidence(
    service, tracker, message, error, reason
):
    socket = QueueSocket()
    socket.messages.put_nowait(message)
    with pytest.raises(error):
        await service.receive("cam-1", "one", socket)
    status = tracker.snapshot("cam-1")
    assert status["status"] == "interrupted"
    assert status["reason_code"] == reason
    assert status["last_frame_received_at"] is None
    assert service.frame_store.get("cam-1") is None


@pytest.mark.asyncio
async def test_ingest_displaced_connection_cannot_overwrite_new_connection(service, tracker):
    old, new = QueueSocket(), QueueSocket()
    old.messages.put_nowait(b"\xff\xd8old\xff\xd9")
    old_task = asyncio.create_task(service.receive("cam-1", "old", old))
    await old.waiting.wait()
    assert tracker.snapshot("cam-1")["status"] == "healthy"
    new_task = asyncio.create_task(service.receive("cam-1", "new", new))
    await new.waiting.wait()
    assert service.hub.latest["cam-1"].jpeg == b"\xff\xd8old\xff\xd9"
    assert tracker.snapshot("cam-1")["reason_code"] == "AWAITING_FRAME"
    old.messages.put_nowait(None)
    await old_task
    assert tracker.snapshot("cam-1")["connection_state"] == "connected"
    new.messages.put_nowait(None)
    await new_task
    assert tracker.snapshot("cam-1")["reason_code"] == "CONNECTION_CLOSED"
