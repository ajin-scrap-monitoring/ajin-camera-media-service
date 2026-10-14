import asyncio
import time

import pytest

from camera_media_service import recording
from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub
from camera_media_service.store import LatestFrameStore


def make_service(tmp_path):
    return recording.RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))


@pytest.mark.asyncio
async def test_policy_and_duplicate_job_survive_restart(tmp_path):
    service = make_service(tmp_path)
    await service.set_policy(
        "camera-main",
        {
            "pre_roll_seconds": 10,
            "post_roll_seconds": 20,
            "retention_days": 2,
        },
    )
    job, created = await service.create_clip("camera-main", "event-1")
    again, duplicate = await service.create_clip("camera-main", "event-1")
    assert created and not duplicate
    assert again == job
    await service.close()
    reopened = make_service(tmp_path)
    try:
        assert (await reopened.get_policy("camera-main"))["pre_roll_seconds"] == 10
        result, created = await reopened.create_clip("camera-main", "event-1")
        assert not created
        assert result["clip_id"] == job["clip_id"]
        assert result["accepted_at"] == job["accepted_at"]
        assert result["status"] == "failed"
        assert result["error"]["code"] == "JOB_INTERRUPTED"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_no_frames_fails_and_delete_keeps_idempotency(tmp_path):
    service = make_service(tmp_path)
    try:
        await service.set_policy(
            "camera-main",
            {
                "pre_roll_seconds": 1,
                "post_roll_seconds": 0,
                "retention_days": 1,
            },
        )
        job, _ = await service.create_clip("camera-main", "empty")
        await service.process_clip(job["clip_id"])
        result = await service.get_clip(job["clip_id"])
        assert result["coverage"] == "none"
        assert result["error"]["code"] == "NO_FRAMES_AVAILABLE"
        await service.delete_clip(job["clip_id"])
        await service.delete_clip(job["clip_id"])
        deleted, created = await service.create_clip("camera-main", "empty")
        assert not created and deleted["status"] == "deleted"
        assert deleted["deleted_at"] is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_active_job_cannot_be_deleted_and_policy_is_snapshot(tmp_path):
    service = make_service(tmp_path)
    try:
        job, _ = await service.create_clip("camera-main", "active")
        await service.set_policy(
            "camera-main",
            {
                "pre_roll_seconds": 0,
                "post_roll_seconds": 1,
                "retention_days": 1,
            },
        )
        with pytest.raises(recording.ApiError) as error:
            await service.delete_clip(job["clip_id"])
        assert error.value.code == "CLIP_IN_PROGRESS"
        assert (await service.get_clip(job["clip_id"]))["recording_policy"][
            "pre_roll_seconds"
        ] == 30
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_disk_buffer_budget_is_bounded(tmp_path):
    service = make_service(tmp_path)
    service.max_buffer_bytes = 8
    try:
        now = time.time()
        for i in range(4):
            await service.store_frame(FrameEnvelope("camera-main", now + i * 0.01, i, b"1234"))
        assert sum(p.stat().st_size for p in (tmp_path / "buffer").glob("*.jpg")) <= 8
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_event_requests_create_one_job(tmp_path):
    service = make_service(tmp_path)
    try:
        results = await asyncio.gather(
            *[service.create_clip("camera-main", "same") for _ in range(10)]
        )
        assert sum(created for _, created in results) == 1
        assert len({job["clip_id"] for job, _ in results}) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_event_frames_are_pinned_when_buffer_budget_is_full(tmp_path):
    service = make_service(tmp_path)
    service.max_buffer_bytes = 4
    try:
        await service.store_frame(FrameEnvelope("camera-main", time.time() - 1, 1, b"1234"))
        await service.create_clip("camera-main", "pin")
        with pytest.raises(OSError):
            await service.store_frame(FrameEnvelope("camera-main", time.time(), 2, b"5678"))
        paths = list((tmp_path / "buffer").glob("*.jpg"))
        assert len(paths) == 1 and paths[0].read_bytes() == b"1234"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_real_encoding_preserves_gaps_and_marks_partial(tmp_path):
    import io

    import imageio_ffmpeg
    from PIL import Image

    service = make_service(tmp_path)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    jpeg = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(jpeg, "JPEG")
    try:
        await service.set_policy(
            "camera-main",
            {
                "pre_roll_seconds": 5,
                "post_roll_seconds": 0,
                "retention_days": 1,
            },
        )
        now = time.time()
        await service.store_frame(FrameEnvelope("camera-main", now - 4, 1, jpeg.getvalue()))
        await service.store_frame(FrameEnvelope("camera-main", now - 1, 2, jpeg.getvalue()))
        job, _ = await service.create_clip("camera-main", "gap")
        await service.process_clip(job["clip_id"])
        result = await service.get_clip(job["clip_id"])
        assert result["status"] == "ready", result
        assert result["coverage"] == "partial"
        assert len(result["gaps"]) >= 1
        reader = imageio_ffmpeg.read_frames(str(service.clips / (job["clip_id"] + ".mp4")))
        metadata = next(reader)
        assert 3 <= metadata["duration"] < 3.2
        reader.close()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_broken_jpeg_produces_failed_job_without_ready_file(tmp_path):
    import imageio_ffmpeg

    service = make_service(tmp_path)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await service.set_policy(
            "camera-main",
            {
                "pre_roll_seconds": 5,
                "post_roll_seconds": 0,
                "retention_days": 1,
            },
        )
        await service.store_frame(
            FrameEnvelope("camera-main", time.time() - 1, 1, b"\xff\xd8bad\xff\xd9")
        )
        job, _ = await service.create_clip("camera-main", "corrupt")
        await service.process_clip(job["clip_id"])
        result = await service.get_clip(job["clip_id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == "ENCODING_FAILED"
        assert not list(service.clips.glob("*.mp4"))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_partial_write_failure_does_not_leave_untracked_disk_usage(tmp_path, monkeypatch):
    from pathlib import Path

    service = make_service(tmp_path)
    original = Path.write_bytes

    def interrupted(path, data):
        original(path, data[:2])
        raise OSError("disk full during write")

    monkeypatch.setattr(Path, "write_bytes", interrupted)
    try:
        with pytest.raises(OSError):
            await service.store_frame(FrameEnvelope("camera-main", time.time(), 1, b"123456"))
        assert list(service.buffer.glob("*.jpg")) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_catalog_write_retries_after_transient_failure(tmp_path, monkeypatch):
    import sqlite3

    service = make_service(tmp_path)
    await service.set_policy(
        "camera-main",
        {
            "pre_roll_seconds": 1,
            "post_roll_seconds": 0,
            "retention_days": 1,
        },
    )
    await service.store_frame(FrameEnvelope("camera-main", time.time() - 0.1, 1, b"jpeg"))
    job, _ = await service.create_clip("camera-main", "save-retry")

    async def encoder(*args):
        # Encoder is isolated here: exercise catalog recovery, not compression.
        (service.clips / (job["clip_id"] + ".mp4")).write_bytes(b"encoded")

    from camera_media_service import encoding

    monkeypatch.setattr(encoding, "encode_clip", encoder)
    original = service._save
    failed = False

    def save(body):
        nonlocal failed
        if body["status"] == "ready" and not failed:
            failed = True
            raise sqlite3.OperationalError("database is temporarily locked")
        original(body)

    monkeypatch.setattr(service, "_save", save)
    try:
        await asyncio.wait_for(service.process_clip(job["clip_id"]), 3)
        assert (await service.get_clip(job["clip_id"]))["status"] == "ready"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_process_waits_for_pending_frame_writer(tmp_path, monkeypatch):
    from camera_media_service import encoding

    service = make_service(tmp_path)
    release = asyncio.Event()
    entered = asyncio.Event()
    original = service.store_frames

    async def slow_store(frame):
        entered.set()
        await release.wait()
        await original(frame)

    async def encoder(*args):
        (service.clips / (job["clip_id"] + ".mp4")).write_bytes(b"encoded")

    monkeypatch.setattr(service, "store_frames", slow_store)
    monkeypatch.setattr(encoding, "encode_clip", encoder)
    await service.set_policy(
        "camera-main",
        {
            "pre_roll_seconds": 1,
            "post_roll_seconds": 0,
            "retention_days": 1,
        },
    )
    await service.start()
    try:
        service.hub.publish("camera-main", b"jpeg")
        await entered.wait()
        await asyncio.sleep(0.005)
        job, _ = await service.create_clip("camera-main", "queued")
        task = asyncio.create_task(service.process_clip(job["clip_id"]))
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 2)
        result = await service.get_clip(job["clip_id"])
        assert result["status"] == "ready"
        assert result["actual_start_at"] is not None
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_successful_expiration_cleanup_is_not_repeated(tmp_path, monkeypatch):
    from camera_media_service.recording import iso

    service = make_service(tmp_path)
    try:
        job, _ = await service.create_clip("camera-main", "cleanup")
        job.update(status="ready", expires_at=iso(time.time() - 1))
        with service.db:
            service._save(job)
        (service.clips / (job["clip_id"] + ".mp4")).write_bytes(b"encoded")
        assert (await service.get_clip(job["clip_id"]))["status"] == "expired"

        async def unavailable(clip_id):
            raise AssertionError("Already removed files must not be revisited")

        monkeypatch.setattr(service, "_remove_files", unavailable)
        await service._expire_all()
        assert (await service.get_clip(job["clip_id"]))["status"] == "expired"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_encoding_barrier_does_not_wait_for_future_frames(tmp_path, monkeypatch):
    from camera_media_service import encoding

    service = make_service(tmp_path)
    first_started, first_release = asyncio.Event(), asyncio.Event()
    future_started, future_release = asyncio.Event(), asyncio.Event()
    original = service.store_frames

    async def slow_store(frame):
        if frame[0].generation == 1:
            first_started.set()
            await first_release.wait()
        else:
            future_started.set()
            await future_release.wait()
        await original(frame)

    async def encoder(*args):
        (service.clips / (job["clip_id"] + ".mp4")).write_bytes(b"encoded")

    monkeypatch.setattr(service, "store_frames", slow_store)
    monkeypatch.setattr(encoding, "encode_clip", encoder)
    await service.set_policy(
        "camera-main",
        {
            "pre_roll_seconds": 1,
            "post_roll_seconds": 0,
            "retention_days": 1,
        },
    )
    await service.start()
    task = None
    try:
        service.hub.publish("camera-main", b"past")
        await first_started.wait()
        await asyncio.sleep(0.005)
        job, _ = await service.create_clip("camera-main", "barrier")
        task = asyncio.create_task(service.process_clip(job["clip_id"]))
        await asyncio.sleep(0.02)
        service.hub.publish("camera-main", b"future")
        first_release.set()
        await future_started.wait()
        await asyncio.wait_for(asyncio.shield(task), 0.5)
        assert (await service.get_clip(job["clip_id"]))["status"] == "ready"
    finally:
        first_release.set()
        future_release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.close()
