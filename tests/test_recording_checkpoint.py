import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from test_recording import make_service

from camera_media_service.pipeline import FrameEnvelope


@pytest.mark.asyncio
async def test_expired_frame_cleanup_is_bounded_per_ingest_turn(tmp_path, monkeypatch):
    import camera_media_service.recording as module

    now = time.time()
    monkeypatch.setattr(module.time, "time", lambda: now)
    service = make_service(tmp_path)
    try:
        await service.store_frames(
            [FrameEnvelope("camera-main", now + i / 30, i, b"jpeg") for i in range(200)]
        )
        async with service.lock:
            await service._prune(now + 400)
        remaining = service.db.execute("SELECT count(*) FROM frames").fetchone()[0]
        assert 100 < remaining < 200
        for _ in range(4):
            async with service.lock:
                await service._prune(now + 400)
        assert not service.db.execute("SELECT 1 FROM frames").fetchone()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_byte_pressure_can_reclaim_more_than_normal_cleanup_batch(tmp_path):
    service = make_service(tmp_path)
    try:
        await service.store_frames(
            [FrameEnvelope("camera-main", time.time() - 400, i, b"jpeg") for i in range(200)]
        )
        service.max_buffer_bytes = 400
        await service.store_frame(FrameEnvelope("camera-main", time.time(), 201, b"x" * 200))
        count, size = service.db.execute("SELECT count(*),sum(size) FROM frames").fetchone()
        assert count == 51 and size == 400
        assert sum(p.stat().st_size for p in service.buffer.glob("*.jpg")) == 400
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_prune_drains_unlink_and_commits_removed_row(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_unlink = Path.unlink

    def delayed(path, *args, **kwargs):
        if path.parent == service.buffer:
            entered.set()
            assert release.wait(3)
        return real_unlink(path, *args, **kwargs)

    async def prune():
        async with service.lock:
            await service._prune(time.time())

    try:
        await service.store_frame(FrameEnvelope("camera-main", time.time() - 400, 1, b"jpeg"))
        monkeypatch.setattr(Path, "unlink", delayed)
        task = asyncio.create_task(prune())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service.db.execute("SELECT 1 FROM frames").fetchone()
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_checkpoint_wait_neither_holds_ingest_lock_nor_closes_live_worker(
    tmp_path, monkeypatch, cancel_close
):
    from camera_media_service.recording_checkpoint import CatalogCheckpointer

    entered, release = threading.Event(), threading.Event()
    original = CatalogCheckpointer._checkpoint

    def blocked(self):
        entered.set()
        assert release.wait(5)
        return original(self)

    monkeypatch.setattr(CatalogCheckpointer, "_checkpoint", blocked)
    service = make_service(tmp_path)
    try:
        assert service.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert service.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        service.checkpointer.min_bytes = 1
        service.checkpointer.interval = 0.01
        await service.start()
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(
            service.store_frame(FrameEnvelope("camera-main", time.time(), 1, b"jpeg")), timeout=1
        )
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 1
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0.05)
        assert not closing.done()
        if cancel_close:
            closing.cancel()
        release.set()
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 3)
        else:
            await asyncio.wait_for(closing, 3)
        assert service.checkpointer.connection is None
        with pytest.raises(sqlite3.ProgrammingError):
            service.db.execute("SELECT 1")
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_reader_defers_checkpoint_without_blocking_durable_frames(tmp_path):
    service = make_service(tmp_path)
    reader = None
    try:
        service.checkpointer.min_bytes = 1
        service.checkpointer.interval = 0.01
        await service.start()
        reader = sqlite3.connect(tmp_path / "catalog.sqlite3")
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM frames").fetchone()
        await service.store_frame(FrameEnvelope("camera-main", time.time(), 1, b"jpeg"))
        deadline = time.monotonic() + 3
        while service.diagnostics.gauges.get("checkpoint_pending_frames", 0) == 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        await asyncio.wait_for(
            service.store_frame(FrameEnvelope("camera-main", time.time(), 2, b"jpeg")), timeout=1
        )
        reader.close()
        reader = None
        while service.diagnostics.gauges.get("checkpoint_pending_frames", 1) != 0:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 2
        assert service.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        if reader:
            reader.close()
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_checkpoint_start_closes_acquired_connection(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = service.checkpointer._connect

    def delayed_connect():
        original()
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(service.checkpointer, "_connect", delayed_connect)
    starting = asyncio.create_task(service.checkpointer.start())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        starting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert service.checkpointer.connection is None
        assert service.checkpointer.task is None
    finally:
        release.set()
        await service.close()
