from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from camera_media_service.ingest import (
    IngestService,
    InvalidJpegFrameError,
    OversizedFrameError,
    TextFrameRejectedError,
)
from camera_media_service.registry import ConnectionRegistry
from camera_media_service.store import LatestFrameStore

VALID_JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"


class FakeWebSocket(AsyncIterator[object]):
    def __init__(self, messages: list[object]) -> None:
        self._messages = iter(messages)
        self.close_calls: list[tuple[int, str]] = []
        self.sent_messages: list[object] = []

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._messages)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))

    async def send(self, message: object) -> None:
        self.sent_messages.append(message)


class BlockingWebSocket(FakeWebSocket):
    def __init__(self, message: object) -> None:
        super().__init__([message])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __anext__(self) -> object:
        self.started.set()
        await self.release.wait()
        return await super().__anext__()


@pytest.mark.asyncio
async def test_connection_replacement_does_not_report_old_socket_as_disconnected():
    service = IngestService(ConnectionRegistry(), LatestFrameStore(), max_frame_bytes=1024)
    transitions = []
    service.hub.connection_observer = lambda *args: transitions.append(args)
    old = BlockingWebSocket(VALID_JPEG)
    old_task = asyncio.create_task(service.receive("cam-1", "old", old))
    await old.started.wait()
    await service.receive("cam-1", "new", FakeWebSocket([VALID_JPEG]))
    old.release.set()
    await old_task
    assert [v[1] for v in transitions] == [True, True, False]


class FailingCloseWebSocket(FakeWebSocket):
    async def close(self, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))
        raise RuntimeError("socket already closed")


class BlockingCloseWebSocket(FakeWebSocket):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def close(self, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_ingest_service_rejects_text_messages_without_storing_frame() -> None:
    store = LatestFrameStore()
    service = IngestService(
        registry=ConnectionRegistry(),
        frame_store=store,
        max_frame_bytes=4 * 1024 * 1024,
    )
    websocket = FakeWebSocket(["not-binary"])

    with pytest.raises(TextFrameRejectedError):
        await service.receive("cam-1", "conn-1", websocket)

    assert store.get("cam-1") is None
    assert websocket.close_calls == [(1003, "binary frames required")]
    assert websocket.sent_messages == []


@pytest.mark.asyncio
async def test_ingest_service_rejects_invalid_jpeg_and_oversized_frames() -> None:
    store = LatestFrameStore()
    invalid_service = IngestService(
        registry=ConnectionRegistry(),
        frame_store=store,
        max_frame_bytes=4 * 1024 * 1024,
    )
    oversized_service = IngestService(
        registry=ConnectionRegistry(),
        frame_store=store,
        max_frame_bytes=8,
    )

    invalid_websocket = FakeWebSocket([b"not-a-jpeg"])
    oversized_websocket = FakeWebSocket([VALID_JPEG])

    with pytest.raises(InvalidJpegFrameError):
        await invalid_service.receive("cam-1", "conn-1", invalid_websocket)

    with pytest.raises(OversizedFrameError):
        await oversized_service.receive("cam-1", "conn-2", oversized_websocket)

    assert store.get("cam-1") is None
    assert invalid_websocket.close_calls == [(1007, "invalid jpeg frame")]
    assert oversized_websocket.close_calls == [(1009, "frame too large")]


@pytest.mark.asyncio
async def test_ingest_service_accepts_a_frame_exactly_at_the_configured_limit() -> None:
    max_frame_bytes = 32
    frame = b"\xff\xd8" + (b"a" * (max_frame_bytes - 4)) + b"\xff\xd9"
    store = LatestFrameStore()
    service = IngestService(
        registry=ConnectionRegistry(),
        frame_store=store,
        max_frame_bytes=max_frame_bytes,
    )

    await service.receive("cam-1", "conn-1", FakeWebSocket([frame]))

    assert store.get("cam-1") == (1, frame)


@pytest.mark.asyncio
async def test_ingest_service_accepts_valid_binary_jpeg_and_replaces_previous_connection() -> None:
    registry = ConnectionRegistry()
    store = LatestFrameStore()
    service = IngestService(
        registry=registry,
        frame_store=store,
        max_frame_bytes=4 * 1024 * 1024,
    )

    stale_websocket = FakeWebSocket([])
    websocket = FakeWebSocket([VALID_JPEG])

    await service.receive("cam-1", "conn-1", stale_websocket)
    await service.receive("cam-1", "conn-2", websocket)

    assert store.get("cam-1") == (1, VALID_JPEG)
    assert registry.release("cam-1", "conn-1") is False
    assert registry.release("cam-1", "conn-2") is False
    assert websocket.close_calls == []
    assert websocket.sent_messages == []


@pytest.mark.asyncio
async def test_newer_camera_connection_evicts_stale_socket_without_releasing_newer_entry() -> None:
    registry = ConnectionRegistry()
    store = LatestFrameStore()
    service = IngestService(
        registry=registry,
        frame_store=store,
        max_frame_bytes=4 * 1024 * 1024,
    )
    stale_websocket = BlockingWebSocket(b"\xff\xd8stale\xff\xd9")
    newer_websocket = FakeWebSocket([b"\xff\xd8newer\xff\xd9"])

    stale_task = asyncio.create_task(service.receive("cam-1", "conn-1", stale_websocket))
    await stale_websocket.started.wait()

    await service.receive("cam-1", "conn-2", newer_websocket)
    stale_websocket.release.set()
    await stale_task

    assert stale_websocket.close_calls == [(1008, "replaced by newer connection")]
    assert store.get("cam-1") == (1, b"\xff\xd8newer\xff\xd9")
    assert registry.is_current("cam-1", "conn-2") is False


@pytest.mark.asyncio
async def test_displaced_socket_close_failure_does_not_orphan_new_connection() -> None:
    registry = ConnectionRegistry()
    store = LatestFrameStore()
    service = IngestService(
        registry=registry,
        frame_store=store,
        max_frame_bytes=4 * 1024 * 1024,
    )
    stale_websocket = FailingCloseWebSocket([])
    newer_websocket = FakeWebSocket([b"\xff\xd8newer\xff\xd9"])
    registry.claim_connection("cam-1", "conn-1", stale_websocket)

    await service.receive("cam-1", "conn-2", newer_websocket)

    assert stale_websocket.close_calls == [(1008, "replaced by newer connection")]
    assert store.get("cam-1") == (1, b"\xff\xd8newer\xff\xd9")
    assert registry.is_current("cam-1", "conn-2") is False


@pytest.mark.asyncio
async def test_cancellation_during_displaced_socket_close_releases_new_connection() -> None:
    registry = ConnectionRegistry()
    service = IngestService(
        registry=registry,
        frame_store=LatestFrameStore(),
        max_frame_bytes=4 * 1024 * 1024,
    )
    blocking_close_websocket = BlockingCloseWebSocket()
    newer_websocket = FakeWebSocket([])
    registry.claim_connection("cam-1", "conn-1", blocking_close_websocket)

    task = asyncio.create_task(service.receive("cam-1", "conn-2", newer_websocket))
    await blocking_close_websocket.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert registry.is_current("cam-1", "conn-2") is False
