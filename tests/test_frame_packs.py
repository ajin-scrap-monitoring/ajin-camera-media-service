import asyncio
import threading
import time

import imageio_ffmpeg
import pytest
from test_daily_recording import jpeg

from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub
from camera_media_service.recording import RecordingService
from camera_media_service.settings import Settings, SettingsError
from camera_media_service.store import LatestFrameStore


def service_at(path, mode="packed", **kwargs):
    return RecordingService(
        path,
        {"camera-main"},
        LiveFrameHub(LatestFrameStore()),
        frame_storage_mode=mode,
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe(),
        **kwargs,
    )


def frames(count=30, *, start=None):
    at = time.time() if start is None else start
    return [FrameEnvelope("camera-main", at + i / 30, i, jpeg()) for i in range(count)]


def test_pack_mode_is_explicit_and_validated():
    env = {"EDGE_AUTH_SECRET": '{"cam":"test"}'}
    assert getattr(Settings.from_env(env), "frame_storage_mode", None) == "files"
    assert Settings.from_env({**env, "FRAME_STORAGE_MODE": "packed"}).frame_storage_mode == "packed"
    with pytest.raises(SettingsError, match="FRAME_STORAGE_MODE"):
        Settings.from_env({**env, "FRAME_STORAGE_MODE": "invalid"})


@pytest.mark.asyncio
async def test_one_pack_and_one_commit_preserve_every_jpeg_and_receipt(tmp_path):
    service = service_at(tmp_path)
    source = frames(60)
    trace = []
    service.db.set_trace_callback(trace.append)
    try:
        await service.store_frames(source)
        assert len(list(service.buffer.glob("*.jpack"))) == 1
        assert not list(service.buffer.glob("*.jpg"))
        assert sum(s == "COMMIT" for s in trace) == 1
        async with service.lock:
            async with service.transaction():
                stored = service.pin_frames(
                    "lease", "camera-main", source[0].received_at, source[-1].received_at + 1
                )
        assert [f.received_at for f in stored] == [f.received_at for f in source]
        assert [service.frame_packs.read(f) for f in stored] == [f.jpeg for f in source]
        assert service.buffer_bytes() == sum(len(f.jpeg) for f in source)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_files_mode_restart_reads_legacy_and_packed_sources(tmp_path):
    source = frames(31, start=time.time() - 2)
    old = service_at(tmp_path, "files")
    await old.store_frames(source[:1])
    await old.close()
    packed = service_at(tmp_path)
    await packed.store_frames(source[1:])
    await packed.close()
    restored = service_at(tmp_path, "files")
    try:
        async with restored.lock:
            async with restored.transaction():
                stored = restored.pin_frames(
                    "lease", "camera-main", source[0].received_at, source[-1].received_at + 1
                )
        assert len(stored) == 31
        assert sum(f.pack is not None for f in stored) == 30
        assert [restored.frame_packs.read(f) for f in stored] == [f.jpeg for f in source]
        from camera_media_service.encoding import encode_daily_segment

        info = await encode_daily_segment(
            restored.ffmpeg,
            restored.buffer,
            tmp_path / "daily.ts",
            stored,
            {"width": 64, "height": 48, "fps": 15},
        )
        assert info["media_info"]["codec"] == "h264" and info["media_info"]["fps"] == 15
        await restored.set_policy(
            "camera-main", {"pre_roll_seconds": 5, "post_roll_seconds": 0, "retention_days": 1}
        )
        job, _ = await restored.create_clip("camera-main", "mixed-source")
        await restored.process_clip(job["clip_id"])
        assert (await restored.get_clip(job["clip_id"]))["status"] == "ready"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_one_pinned_member_keeps_whole_pack_and_physical_budget(tmp_path):
    source = frames(3)
    size = sum(len(f.jpeg) for f in source)
    service = service_at(tmp_path, max_buffer_bytes=size)
    try:
        await service.store_frames(source)
        async with service.lock:
            async with service.transaction():
                pinned = service.pin_frames(
                    "event", "camera-main", source[0].received_at, source[1].received_at
                )
            await service._prune(time.time() + 400)
        assert len(pinned) == 1
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 1
        assert service.buffer_bytes() == size
        assert service.frame_packs.read(pinned[0]) == source[0].jpeg
        with pytest.raises(OSError):
            await service.store_frames(frames(1))
        async with service.lock:
            async with service.transaction():
                service.release_frames("event")
            await service._prune(time.time() + 400)
        assert service.buffer_bytes() == 0 and not list(service.buffer.glob("*.jpack"))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_pack_fsync_does_not_commit_frames_or_leave_bytes(tmp_path, monkeypatch):
    import camera_media_service.frame_packs as module

    service = service_at(tmp_path)
    monkeypatch.setattr(
        module.os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("disk failed"))
    )
    try:
        with pytest.raises(OSError, match="disk failed"):
            await service.store_frames(frames())
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
        assert not list(service.buffer.iterdir())
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_orphan_and_incomplete_packs_are_reconciled_without_deleting_referenced(tmp_path):
    service = service_at(tmp_path)
    await service.store_frames(frames())
    saved = list(service.buffer.glob("*.jpack"))[0]
    await service.close()
    orphan = saved.parent / ("a" * 32 + ".jpack")
    unfinished = saved.parent / ("b" * 32 + ".tmp.pack")
    orphan.write_bytes(b"uncommitted")
    unfinished.write_bytes(b"partial")
    reopened = service_at(tmp_path, "files")
    try:
        assert saved.exists() and not orphan.exists() and not unfinished.exists()
        assert reopened.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 30
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_recovery_never_deletes_a_pack_still_named_by_frame_ranges(tmp_path):
    service = service_at(tmp_path)
    source = frames(3)
    await service.store_frames(source)
    name = next(service.buffer.glob("*.jpack"))
    with service.db:
        service.db.execute("DELETE FROM frame_packs")
    await service.close()
    reopened = service_at(tmp_path, "files")
    try:
        assert name.exists()
        assert reopened.buffer_bytes() == sum(len(f.jpeg) for f in source)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_commit_failure_removes_unindexed_pack(tmp_path, monkeypatch):
    import sqlite3

    from camera_media_service import media_files

    service = service_at(tmp_path)
    original = media_files.storage_io

    async def failed_commit(function, *a, **kw):
        if getattr(function, "__self__", None) is service.db and function.__name__ == "commit":
            raise sqlite3.OperationalError("injected commit failure")
        return await original(function, *a, **kw)

    monkeypatch.setattr(media_files, "storage_io", failed_commit)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected commit"):
            await service.store_frames(frames())
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
        assert not list(service.buffer.iterdir())
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancellation_after_commit_preserves_committed_pack(tmp_path, monkeypatch):
    from camera_media_service import media_files

    service = service_at(tmp_path)
    original = media_files.storage_io

    async def interrupted_commit(function, *a, **kw):
        result = await original(function, *a, **kw)
        if getattr(function, "__self__", None) is service.db and function.__name__ == "commit":
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(media_files, "storage_io", interrupted_commit)
    source = frames()
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.store_frames(source)
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == len(source)
        assert len(list(service.buffer.glob("*.jpack"))) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_truncated_pack_cannot_publish_a_ready_video(tmp_path):
    from camera_media_service.encoding import encode_daily_segment

    service = service_at(tmp_path)
    try:
        source = frames()
        await service.store_frames(source)
        async with service.lock:
            async with service.transaction():
                stored = service.pin_frames(
                    "daily", "camera-main", source[0].received_at, source[-1].received_at + 1
                )
        next(service.buffer.glob("*.jpack")).write_bytes(b"truncated")
        with pytest.raises(OSError, match="Truncated"):
            await encode_daily_segment(
                service.ffmpeg,
                service.buffer,
                tmp_path / "bad.ts",
                stored,
                {"width": 64, "height": 48, "fps": 15},
            )
        assert not (tmp_path / "bad.ts").exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_pack_write_drains_worker_before_cleanup(tmp_path, monkeypatch):
    service = service_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = service.frame_packs.write

    def delayed(*a):
        entered.set()
        assert release.wait(10)
        return original(*a)

    monkeypatch.setattr(service.frame_packs, "write", delayed)
    task = asyncio.create_task(service.store_frames(frames()))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(service.buffer.iterdir())
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_writer_batches_real_paced_input_and_shutdown_drains(tmp_path):
    service = service_at(tmp_path)
    await service.start()
    try:
        for _ in range(60):
            service.hub.publish("camera-main", jpeg())
            await asyncio.sleep(1 / 30)
        await service.close()
        import sqlite3

        with sqlite3.connect(tmp_path / "catalog.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM frames").fetchone()[0] == 60
        assert len(list(service.buffer.glob("*.jpack"))) <= 6
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_large_direct_batch_splits_packs_without_extra_catalogue_commits(tmp_path):
    service = service_at(tmp_path)
    data = b"x" * (2 * 1024**2)
    source = [FrameEnvelope("camera-main", time.time() + i / 30, i, data) for i in range(5)]
    trace = []
    service.db.set_trace_callback(trace.append)
    try:
        await service.store_frames(source)
        assert len(list(service.buffer.glob("*.jpack"))) == 2
        assert max(p.stat().st_size for p in service.buffer.glob("*.jpack")) <= 8 * 1024**2
        assert sum(s == "COMMIT" for s in trace) == 1
        assert service.buffer_bytes() == 5 * len(data)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_writer_byte_bound_does_not_dequeue_oversized_next_frame(tmp_path):
    service = service_at(tmp_path)
    data = b"x" * (3 * 1024**2)
    await service.start()
    try:
        for _ in range(5):
            service.hub.publish("camera-main", data)
        await service.queue.wait_received()
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 5
        assert len(list(service.buffer.glob("*.jpack"))) == 3
        assert max(p.stat().st_size for p in service.buffer.glob("*.jpack")) <= 8 * 1024**2
        assert service.queue._unfinished_tasks == 0
    finally:
        await service.close()
