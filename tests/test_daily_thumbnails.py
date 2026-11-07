import asyncio
from datetime import date

import imageio_ffmpeg
import pytest
from test_daily_export import make_exports
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope
from camera_media_service.recording import ApiError, iso
from camera_media_service.thumbnails import ThumbnailService


async def active_fixture(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 11, 7))
    clock = [window.start_at + 60]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(clock[0])
    exports = make_exports(daily)
    thumbnail = ThumbnailService(recording, daily, exports)
    await thumbnail.start()
    return recording, daily, exports, thumbnail, clock


async def add_slot(recording, daily, clock):
    start = clock[0]
    raw = jpeg()
    for i in range(30):
        await recording.store_frame(FrameEnvelope("camera-main", start + i / 30, i, raw))
    clock[0] += 10
    await daily.tick(clock[0])
    return (await daily.list_days("camera-main", {}, 5, None))[0]


async def ready(thumbnail, key):
    for _ in range(200):
        state = await thumbnail.info("day", key)
        if state["status"] == "ready":
            return state
        await asyncio.sleep(0.02)
    pytest.fail(str(state))


@pytest.mark.asyncio
async def test_active_poll_rotates_beyond_one_page_without_historical_scan(tmp_path, monkeypatch):
    recording, daily, exports, thumbnail, clock = await active_fixture(tmp_path, monkeypatch)
    thumbnail.loop_task.cancel()
    await asyncio.gather(thumbnail.loop_task, return_exceptions=True)
    window = DailyWindow.from_date(date(2026, 11, 7))
    with recording.db:
        keys = {
            daily.catalog.ensure_day(f"extra-{i}", window, 30)["recording_id"] for i in range(65)
        }
    seen = set()
    monkeypatch.setattr(thumbnail, "notify_ready", lambda kind, key, identity: seen.add(key))
    try:
        await thumbnail._poll_active_dates()
        await thumbnail._poll_active_dates()
        assert keys <= seen
    finally:
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_capacity_deferral_preserves_automatic_retry_budget(tmp_path, monkeypatch):
    recording, daily, exports, thumbnail, clock = await active_fixture(tmp_path, monkeypatch)
    thumbnail.available = False
    day = await add_slot(recording, daily, clock)
    thumbnail.available = True
    key = day["recording_id"]
    _, _, identity, _ = await thumbnail._snapshot("day", key)
    row = thumbnail._base("day", key, "camera-main", "failed", "GENERATION_FAILED")
    row.update(_source_identity=identity, _auto_attempts=2, _auto_next_at=0)
    await thumbnail.store.put("day", key, row)
    occupied = asyncio.create_task(asyncio.Event().wait())
    thumbnail.jobs["fixture-full-slot"] = occupied
    thumbnail.max_jobs = 1
    thumbnail.notify_ready("day", key, None)
    try:
        for _ in range(100):
            value = await thumbnail.store.get("day", key)
            if value.get("reason_code") == "CAPACITY_EXCEEDED":
                break
            await asyncio.sleep(0.02)
        assert value["reason_code"] == "CAPACITY_EXCEEDED"
        assert value["_auto_attempts"] == 2 and value["_auto_next_at"] == 0
    finally:
        thumbnail.jobs.pop("fixture-full-slot", None)
        occupied.cancel()
        await asyncio.gather(occupied, return_exceptions=True)
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_first_ready_ts_automatically_prepares_one_active_date_image(tmp_path, monkeypatch):
    recording, daily, exports, thumbnail, clock = await active_fixture(tmp_path, monkeypatch)
    restart = None
    try:
        day = await add_slot(recording, daily, clock)
        key = day["recording_id"]
        info = await ready(thumbnail, key)
        assert day["status"] == "recording" and day["archive_state"] == "segments"
        assert info["representative_at"] == iso(clock[0] - 10)
        assert info["time_basis"] == "server_received_at"
        assert info["camera_id"] == "camera-main" and info["recording_id"] == key
        path = recording.root / "daily-thumbnails" / (key + ".jpg")
        before = path.stat().st_mtime_ns
        assert not list(exports.output.glob("*.mp4")) and not exports.jobs
        await add_slot(recording, daily, clock)
        await asyncio.sleep(0.3)
        assert path.stat().st_mtime_ns == before
        await thumbnail.close()
        restart = ThumbnailService(recording, daily, exports)
        await restart.start()
        restart.ffmpeg = "must-reuse-the-existing-image"
        assert await restart.info("day", key) == info
        file, size = await restart.open_image("day", key)
        try:
            assert file.read(2) == b"\xff\xd8" and size == info["size_bytes"]
        finally:
            file.close()
    finally:
        if restart:
            await restart.close()
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_automatic_failures_are_bounded_and_manual_retry_is_explicit(tmp_path, monkeypatch):
    import camera_media_service.thumbnails as module
    from camera_media_service.thumbnail_media import ThumbnailFailure

    recording, daily, exports, thumbnail, clock = await active_fixture(tmp_path, monkeypatch)
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise ThumbnailFailure("GENERATION_FAILED")

    monkeypatch.setattr(module, "generate_thumbnail", fail)
    try:
        day = await add_slot(recording, daily, clock)
        key = day["recording_id"]
        for attempt in range(1, 4):
            if attempt > 1:
                clock[0] += 301
                await thumbnail.prepare("day", key, retry_failed=True, _automatic=True)
            for _ in range(100):
                row = await thumbnail.store.get("day", key)
                if row and row["status"] == "failed" and row.get("_auto_attempts") == attempt:
                    break
                await asyncio.sleep(0.02)
            assert row["status"] == "failed" and row["_auto_attempts"] == attempt
        assert len(calls) == 3
        clock[0] += 301
        await thumbnail._poll_active_dates()
        _, created = await thumbnail.prepare("day", key, retry_failed=True, _automatic=True)
        assert not created and len(calls) == 3
        _, created = await thumbnail.prepare("day", key, retry_failed=True)
        assert created
    finally:
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_midnight_uses_a_new_date_id_and_does_not_reuse_yesterday_image(
    tmp_path, monkeypatch
):
    recording, daily, exports, thumbnail, clock = await active_fixture(tmp_path, monkeypatch)
    try:
        old = await add_slot(recording, daily, clock)
        old_info = await ready(thumbnail, old["recording_id"])
        clock[0] = DailyWindow.from_date(date(2026, 11, 8)).start_at
        await daily.tick(clock[0])
        new = (await daily.list_days("camera-main", {}, 5, None))[0]
        assert new["recording_id"] != old["recording_id"]
        assert (await thumbnail.info("day", new["recording_id"]))["status"] == "unavailable"
        new = await add_slot(recording, daily, clock)
        new_info = await ready(thumbnail, new["recording_id"])
        assert new_info["recording_id"] != old_info["recording_id"]
        assert new_info["content_path"] != old_info["content_path"]
        assert new_info["representative_at"].startswith("2026-11-07T15:00:")
        assert (await thumbnail.info("day", old["recording_id"]))["content_path"] == old_info[
            "content_path"
        ]
    finally:
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_delete_during_ts_decode_drains_lease_and_cannot_publish_again(tmp_path, monkeypatch):
    from test_daily_export import recorded_day

    import camera_media_service.thumbnails as module

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    thumbnail = ThumbnailService(recording, daily, exports)
    await thumbnail.start()
    entered, release = asyncio.Event(), asyncio.Event()
    deletion = None

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await release.wait()

    monkeypatch.setattr(module, "generate_thumbnail", blocked)
    try:
        key = day["recording_id"]
        await thumbnail.prepare("day", key)
        await asyncio.wait_for(entered.wait(), 1)
        assert recording.db.execute(
            "SELECT 1 FROM clip_recording_pins WHERE day_id=?", (key,)
        ).fetchone()
        deletion = asyncio.create_task(exports.delete_day(key))
        await asyncio.sleep(0.03)
        assert not deletion.done()
        release.set()
        await asyncio.wait_for(deletion, 2)
        with pytest.raises(ApiError) as error:
            await thumbnail.open_image("day", key)
        assert error.value.status == 410
        assert not (recording.root / "daily-thumbnails" / (key + ".jpg")).exists()
        assert not recording.db.execute(
            "SELECT 1 FROM clip_recording_pins WHERE day_id=?", (key,)
        ).fetchone()
        await exports.delete_day(key)
    finally:
        release.set()
        if deletion:
            await asyncio.gather(deletion, return_exceptions=True)
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_queued_ts_reader_cannot_enter_during_source_compaction(tmp_path, monkeypatch):
    from test_daily_archive import archive_ready
    from test_daily_export import recorded_day

    import camera_media_service.thumbnails as module

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    thumbnail = ThumbnailService(recording, daily, exports)
    await thumbnail.start()
    entered, proceed, decoded, release = (asyncio.Event() for _ in range(4))
    original = exports._compact_files
    generate = module.generate_thumbnail
    compact = None
    await thumbnail.slot.acquire()

    async def blocked_compaction(folder):
        entered.set()
        await proceed.wait()
        return await original(folder)

    async def tracked_decode(*args, **kwargs):
        decoded.set()
        await release.wait()
        return await generate(*args, **kwargs)

    monkeypatch.setattr(exports, "_compact_files", blocked_compaction)
    monkeypatch.setattr(module, "generate_thumbnail", tracked_decode)
    try:
        await thumbnail.prepare("day", day["recording_id"])
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, day["recording_id"]))["archive_state"] == "mp4"
        compact = asyncio.create_task(exports._compact(day["recording_id"]))
        await asyncio.wait_for(entered.wait(), 1)
        thumbnail.slot.release()
        await asyncio.sleep(0.1)
        assert not decoded.is_set(), "A TS reader acquired a source lease after compaction started"
    finally:
        proceed.set()
        release.set()
        if compact:
            await asyncio.gather(compact, return_exceptions=True)
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_pressure_defers_then_active_date_poll_retries(tmp_path, monkeypatch):
    recording, daily, exports, thumbnail, clock = await active_fixture(tmp_path, monkeypatch)
    recording.queue = asyncio.Queue(1200)
    for _ in range(80):
        recording.queue.put_nowait(None)
    try:
        # Do not call tick's receipt barrier with an artificial queue.
        recording.queue, held = None, recording.queue
        thumbnail.available = False
        day = await add_slot(recording, daily, clock)
        thumbnail.available = True
        recording.queue = held
        info, created = await thumbnail.prepare("day", day["recording_id"])
        assert not created and info["status"] == "unavailable"
        assert info["reason_code"] == "RECORDING_BUSY"
        assert not thumbnail.jobs and not recording.capacity_reservations
        recording.queue = None
        await thumbnail._poll_active_dates()
        assert (await ready(thumbnail, day["recording_id"]))["status"] == "ready"
    finally:
        recording.queue = None
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_date_image_survives_archive_and_ts_compaction_then_expires(tmp_path, monkeypatch):
    from test_daily_archive import archive_ready
    from test_daily_export import recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    thumbnail = ThumbnailService(recording, daily, exports)
    await thumbnail.start()
    key = day["recording_id"]
    try:
        info, created = await thumbnail.prepare("day", key)
        assert created
        info = await ready(thumbnail, key)
        path = recording.root / "daily-thumbnails" / (key + ".jpg")
        before = path.read_bytes(), path.stat().st_mtime_ns
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["archive_state"] == "mp4"
        for _ in range(30):
            await exports.maintenance(clock[0])
            if not (recording.root / "daily" / key).exists():
                break
            await asyncio.sleep(0.02)
        assert not (recording.root / "daily" / key).exists()
        assert await thumbnail.info("day", key) == info
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        file, _ = await thumbnail.open_image("day", key)
        file.close()
        clock[0] += 31 * 86400
        for operation in (thumbnail.info, thumbnail.open_image):
            with pytest.raises(ApiError) as error:
                await operation("day", key)
            assert error.value.status == 410
    finally:
        await thumbnail.close()
        await exports.close()
        await daily.close()
        await recording.close()
