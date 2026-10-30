import asyncio

import pytest
from test_recorded_clips import cleanup, segment, setup

from camera_media_service.recording import ApiError, iso


@pytest.mark.asyncio
async def test_legacy_request_winning_during_range_space_scan_reports_conflict(
    tmp_path, monkeypatch
):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    segment(daily, now - 30, now)
    entered, release = asyncio.Event(), asyncio.Event()

    async def scan(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(daily, "check_space", scan)
    task = asyncio.create_task(
        clips.create_range("camera-main", "collision", iso(now - 20), iso(now - 10))
    )
    try:
        await entered.wait()
        await recording.create_clip("camera-main", "collision")
        release.set()
        with pytest.raises(ApiError) as error:
            await task
        assert error.value.status == 409 and error.value.code == "IDEMPOTENCY_CONFLICT"
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not daily.storage_reservations
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(recording, daily, clips)


async def completed(recording, key):
    for _ in range(500):
        result = await recording.get_clip(key)
        if result["status"] in ("ready", "failed"):
            return result
        await asyncio.sleep(0.01)
    pytest.fail("Range extraction did not finish")


@pytest.mark.asyncio
async def test_real_range_extraction_and_explicit_failed_retry(tmp_path, monkeypatch):
    import imageio_ffmpeg
    from test_clip_media import make_ts

    import camera_media_service.clip_media as module

    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    recording.encoding_slot.release()
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    now = clock[0]
    _, item = segment(daily, now - 6, now)
    path = daily.segment_path(item)
    make_ts(tmp_path).replace(path)
    with daily.db:
        daily.catalog.complete_segment(
            item["segment_id"],
            {
                "path": str(path.relative_to(tmp_path)),
                "size_bytes": path.stat().st_size,
                "duration": 6.0,
                "profile": {"width": 160, "height": 120, "fps": 10},
            },
        )
    real = module.remux_clip

    async def fail(*args, **kwargs):
        raise OSError("Injected transient read failure")

    monkeypatch.setattr(module, "remux_clip", fail)
    try:
        start, end = iso(now - 4.5), iso(now - 1.2)
        body, _ = await clips.create_range("camera-main", "retry", start, end)
        failed = await completed(recording, body["clip_id"])
        assert failed["status"] == "failed"
        repeated, created = await clips.create_range("camera-main", "retry", start, end)
        assert not created and repeated["attempt_count"] == 1
        monkeypatch.setattr(module, "remux_clip", real)
        retried, created = await clips.create_range(
            "camera-main", "retry", start, end, retry_failed=True
        )
        assert created and retried["clip_id"] == body["clip_id"]
        ready = await completed(recording, body["clip_id"])
        assert ready["status"] == "ready", ready
        assert ready["attempt_count"] == 2
        assert ready["actual_start_at"] == iso(now - 5)
        assert ready["actual_end_at"] == end
        assert ready["size_bytes"] == (recording.clips / (body["clip_id"] + ".mp4")).stat().st_size
        assert ready["media_info"]["width"] == 160
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not daily.storage_reservations
        await recording.delete_clip(body["clip_id"])
        deleted, created = await clips.create_range(
            "camera-main", "retry", start, end, retry_failed=True
        )
        assert not created and deleted["status"] == "deleted"
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_request_range_uses_exact_bounds_without_changing_camera_policy(
    tmp_path, monkeypatch
):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    segment(daily, now - 120, now)
    before = await recording.get_policy("camera-main")
    try:
        body, created = await clips.create_range(
            "camera-main", "range", iso(now - 100), iso(now - 10)
        )
        assert created and body["request_mode"] == "time_range"
        assert body["requested_start_at"] == iso(now - 100)
        assert body["requested_end_at"] == iso(now - 10)
        assert body["coverage"] == "complete"
        assert body["attempt_count"] == 1
        assert await recording.get_policy("camera-main") == before
        same, created = await clips.create_range(
            "camera-main", "range", iso(now - 100), iso(now - 10)
        )
        assert not created and same["clip_id"] == body["clip_id"]
        with pytest.raises(ApiError) as error:
            await clips.create_range("camera-main", "range", iso(now - 90), iso(now - 10))
        assert error.value.status == 409 and error.value.code == "IDEMPOTENCY_CONFLICT"
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_range_without_start_footage_retains_available_middle_and_future_gap(
    tmp_path, monkeypatch
):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    segment(daily, now - 20, now - 5)
    try:
        body, _ = await clips.create_range("camera-main", "partial", iso(now - 100), iso(now + 10))
        assert body["coverage"] == "partial"
        assert body["actual_start_at"] == iso(now - 20)
        assert body["actual_end_at"] == iso(now - 5)
        assert body["gaps"][0]["start_at"] == iso(now - 100)
        assert body["gaps"][-1]["reason_code"] == "FUTURE_NOT_RECORDED"
        assert body["gaps"][-1]["end_at"] == iso(now + 10)
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,status",
    [
        ("reversed", 400),
        ("long", 400),
        ("future", 400),
        ("missing", 404),
        ("pending", 409),
        ("bad_retry", 400),
    ],
)
async def test_range_validation_and_no_available_media(tmp_path, monkeypatch, kind, status):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    start, end, retry = now - 30, now - 10, False
    if kind == "reversed":
        end = start
    if kind == "long":
        start = now - 3601
        end = now
    if kind == "future":
        start = now + 1
        end = now + 5
    if kind == "pending":
        segment(daily, now - 30, now, "processing")
    if kind == "bad_retry":
        retry = "yes"
    try:
        with pytest.raises(ApiError) as error:
            await clips.create_range(
                "camera-main", "invalid", iso(start), iso(end), retry_failed=retry
            )
        assert error.value.status == status
        assert not recording.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not daily.storage_reservations
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_concurrent_range_duplicates_create_one_pinned_job(tmp_path, monkeypatch):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    segment(daily, now - 30, now)
    try:
        results = await asyncio.gather(
            *[
                clips.create_range("camera-main", "same", iso(now - 20), iso(now - 10))
                for _ in range(8)
            ]
        )
        assert sum(created for _, created in results) == 1
        assert len({body["clip_id"] for body, _ in results}) == 1
        assert recording.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 1
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_cancelled_space_admission_releases_pins_without_blocking_ingest_lock(
    tmp_path, monkeypatch
):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    segment(daily, now - 30, now)
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(daily, "check_space", blocked)
    task = asyncio.create_task(
        clips.create_range("camera-main", "cancel", iso(now - 20), iso(now - 10))
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(recording.get_policy("camera-main"), 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not recording.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not daily.storage_reservations
        assert not recording.db.execute("SELECT 1 FROM jobs").fetchone()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(recording, daily, clips)
