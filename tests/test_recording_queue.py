import asyncio

import pytest
from test_recording import make_service

from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub, RecordingQueue
from camera_media_service.store import LatestFrameStore


@pytest.mark.asyncio
async def test_byte_pressure_evicts_enough_oldest_receipts_and_releases_barrier():
    hub = LiveFrameHub(LatestFrameStore())
    losses = []
    hub.loss_observer = lambda *args: losses.append(args)
    queue = hub.subscribe(maxsize=10, recording=True, max_bytes=10)
    for _ in range(3):
        hub.publish("cam", b"123")
    barrier = asyncio.create_task(queue.wait_received())
    await asyncio.sleep(0)
    newest = hub.publish("cam", b"12345678")
    await asyncio.wait_for(barrier, 1)
    assert len(losses) == 3
    assert queue.queued_bytes == 8 and queue.qsize() == 1
    assert queue.get_nowait() is newest
    assert queue.queued_bytes == 0
    assert queue.pending_timestamps("cam") == [newest.received_at]
    queue.acknowledge(newest)
    await asyncio.wait_for(queue.join(), 1)


def test_oversize_receipt_does_not_evict_existing_recording_or_live_delivery():
    hub = LiveFrameHub(LatestFrameStore())
    losses = []
    hub.loss_observer = lambda *args: losses.append(args)
    recording = hub.subscribe(maxsize=10, recording=True, max_bytes=5)
    live = hub.subscribe(maxsize=1)
    kept = hub.publish("cam", b"123")
    rejected = hub.publish("cam", b"123456")
    assert recording.get_nowait() is kept
    assert live.get_nowait() is rejected
    assert len(losses) == 1 and losses[0][1] == rejected.received_at
    assert recording.queued_bytes == 0
    recording.acknowledge(kept)


def test_queue_rejects_byte_overflow_even_when_frame_count_fits():
    queue = RecordingQueue(10, max_bytes=5)
    queue.put_nowait(FrameEnvelope("cam", 1, 1, b"123"))
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(FrameEnvelope("cam", 2, 2, b"123"))
    assert queue.qsize() == 1 and queue.queued_bytes == 3
    assert queue.pending_timestamps("cam") == [1]


@pytest.mark.asyncio
async def test_storage_stall_buffers_ten_seconds_and_close_drains_receipts(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    real_store = service.store_frames

    async def delayed(frames):
        entered.set()
        await release.wait()
        await real_store(frames)

    monkeypatch.setattr(service, "store_frames", delayed)
    losses = []
    service.hub.loss_observer = lambda *args: losses.append(args)
    closed = None
    try:
        await service.start()
        service.hub.publish("camera-main", b"jpeg" * 19000)
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(300):  # Ten seconds at the actual 30fps input rate.
            service.hub.publish("camera-main", b"jpeg" * 19000)
        assert service.queue.qsize() == 300
        assert service.queue.queued_bytes == 300 * 76000
        assert losses == []
        closed = asyncio.create_task(service.close())
        await asyncio.sleep(0.02)
        assert not closed.done()
        release.set()
        await asyncio.wait_for(closed, 10)
        import sqlite3

        with sqlite3.connect(tmp_path / "catalog.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM frames").fetchone()[0] == 301
        assert service.queue.queued_bytes == 0
    finally:
        release.set()
        if closed is not None:
            await closed
        else:
            await service.close()


@pytest.mark.asyncio
async def test_close_reports_dead_writer_and_does_not_wait_forever(tmp_path):
    service = make_service(tmp_path)
    await service.start()
    writer = service.tasks[0]
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)
    service.hub.publish("camera-main", b"jpeg")
    with pytest.raises(RuntimeError, match="writer stopped"):
        await asyncio.wait_for(service.close(), 1)
    assert service.queue.qsize() == 0 and service.queue.queued_bytes == 0
    await asyncio.wait_for(service.queue.join(), 1)
    assert service.diagnostics.loss_counts["shutdown_writer_unavailable"] == 1
