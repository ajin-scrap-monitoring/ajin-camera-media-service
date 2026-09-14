from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub
from camera_media_service.storage import (
    FrameMeta,
    LocalStorageBackend,
    RingBufferIndex,
    SegmentWriter,
)
from camera_media_service.store import LatestFrameStore

VALID_JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"


@pytest.mark.asyncio
async def test_local_storage_backend_writes_frame(tmp_path: Path) -> None:
    backend = LocalStorageBackend(media_root=str(tmp_path))
    envelope = FrameEnvelope(
        camera_id="cam-1",
        received_at=1700000000.0,
        generation=1,
        jpeg=VALID_JPEG,
    )

    path_str = await backend.write_frame(envelope)
    saved_path = Path(path_str)

    assert saved_path.exists()
    assert saved_path.read_bytes() == VALID_JPEG
    assert "cam-1" in saved_path.parts


def test_ring_buffer_index_queries_by_camera_and_time() -> None:
    index = RingBufferIndex()
    meta1 = FrameMeta("cam-1", 1, 100.0, "/tmp/f1.jpg", 10)
    meta2 = FrameMeta("cam-1", 2, 200.0, "/tmp/f2.jpg", 10)
    meta3 = FrameMeta("cam-2", 1, 150.0, "/tmp/f3.jpg", 10)

    index.add(meta1)
    index.add(meta2)
    index.add(meta3)

    assert index.count("cam-1") == 2
    assert index.query("cam-1", start_time=150.0) == [meta2]
    assert index.query("cam-1", end_time=150.0) == [meta1]


@pytest.mark.asyncio
async def test_segment_writer_consumes_and_indexes_frames(tmp_path: Path) -> None:
    store = LatestFrameStore()
    hub = LiveFrameHub(frame_store=store)
    backend = LocalStorageBackend(media_root=str(tmp_path))
    index = RingBufferIndex()

    writer = SegmentWriter(hub=hub, backend=backend, index=index)
    writer.start()

    envelope = hub.publish("cam-1", VALID_JPEG)

    # Wait briefly for background async segment writer task
    for _ in range(50):
        if index.count("cam-1") > 0:
            break
        await asyncio.sleep(0.01)

    assert index.count("cam-1") == 1
    stored = index.query("cam-1")[0]
    assert stored.generation == envelope.generation
    assert Path(stored.path).exists()

    await writer.stop()
