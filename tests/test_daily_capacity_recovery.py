import asyncio
import threading
from datetime import date

import imageio_ffmpeg
import pytest
from test_daily_archive import archive_ready
from test_daily_export import make_exports, recorded_day
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope, RecordingQueue
from camera_media_service.recording import ApiError


@pytest.mark.asyncio
async def test_capacity_scan_rechecks_reservations_changed_during_worker(tmp_path, monkeypatch):
    import camera_media_service.daily_recording as module

    clock = [DailyWindow.from_date(date(2026, 11, 9)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    daily.settings.max_daily_storage_bytes = 10000
    entered, release = threading.Event(), threading.Event()
    original = module.iter_regular_files

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(module, "iter_regular_files", blocked)
    task = asyncio.create_task(daily.check_space())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        daily.storage_reservations["new-encoder"] = 20000
        release.set()
        with pytest.raises(ApiError) as failure:
            await task
        assert failure.value.condition == "daily_budget"
        assert failure.value.budget["unallocated_reserved_bytes"] == 20000
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        daily.storage_reservations.clear()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_capacity_denial_keeps_cursor_and_boundary_frames_until_recovery(
    tmp_path, monkeypatch, restart
):
    start = DailyWindow.from_date(date(2026, 11, 9)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    raw = jpeg()
    try:
        await daily.tick(start)
        source = [FrameEnvelope("camera-main", start - 1 / 30 + i / 30, i, raw) for i in range(302)]
        await service.store_frames(source)
        daily.settings.max_daily_storage_bytes = 1
        clock[0] = start + 10
        await daily.tick(clock[0])
        cursor = service.db.execute("SELECT cursor FROM daily_cursors").fetchone()[0]
        assert cursor == start, "Transient admission must not finalize a missing interval"
        assert not service.db.execute(
            "SELECT 1 FROM daily_gaps WHERE reason='STORAGE_FULL'"
        ).fetchone()
        assert service.daily_frame_watermarks["camera-main"] <= source[0].received_at
        async with service.lock:
            await service._prune(start + 400)
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == len(source)
        # A second admission failure reuses the same unpublished attempt.
        daily._capacity_retry_at.clear()
        await daily.tick(clock[0])
        assert service.db.execute("SELECT count(*) FROM daily_segments").fetchone()[0] == 1
        if restart:
            await daily.close()
            await service.close()
            service, daily = make_daily(tmp_path, clock, monkeypatch)
            service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        daily.settings.max_daily_storage_bytes = 100_000_000
        daily._capacity_retry_at.clear()
        await daily.tick(clock[0])
        assert service.db.execute("SELECT cursor FROM daily_cursors").fetchone()[0] == start + 10
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert day["recorded_seconds"] == pytest.approx(10, abs=0.01)
        assert not day["gaps"]
        assert (
            service.db.execute(
                "SELECT count(*) FROM daily_segments WHERE status='ready'"
            ).fetchone()[0]
            == 1
        )
        assert not daily.capacity_pending
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_archive_workspace_owner_survives_verified_output_until_compaction(
    tmp_path, monkeypatch, restart
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    key = old["recording_id"]
    with service.db:
        service.db.execute("INSERT INTO clip_recording_pins VALUES('hold',?)", (key,))
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["export_state"] == "ready"
        while exports.jobs:
            await asyncio.sleep(0)
        assert key in daily.storage_reservation_outputs, "Compaction still owns duplicate workspace"
        daily.settings.daily_archive_workspace_bytes = 1_000_000
        folders = ("daily", "daily-exports", "daily-playback", "daily-thumbnails")
        used = sum(
            p.stat().st_size for f in folders for p in (service.root / f).rglob("*") if p.is_file()
        )
        daily.settings.max_daily_storage_bytes = used + 100
        await daily.check_space(recording=True)
        if restart:
            await exports.close()
            await daily.close()
            await service.close()
            service, daily = make_daily(tmp_path, clock, monkeypatch)
            exports = make_exports(daily)
            daily.settings.daily_archive_workspace_bytes = 1_000_000
            daily.settings.max_daily_storage_bytes = used + 100
            assert key in daily.storage_reservation_outputs
            await daily.check_space(recording=True)
        with pytest.raises(ApiError):
            await daily.check_space(101, recording=True)
        # Keep the lease when a reader prevents actual source removal.
        await exports.maintenance(clock[0])
        assert key in daily.storage_reservation_outputs
        with service.db:
            service.db.execute("DELETE FROM clip_recording_pins WHERE clip_id='hold'")
        await exports.maintenance(clock[0])
        assert key not in daily.storage_reservation_outputs
        assert not service.db.execute("SELECT 1 FROM daily_compactions").fetchone()
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_post_encode_limit_denial_keeps_unpublished_output_out_of_catalogue(
    tmp_path, monkeypatch
):
    import camera_media_service.daily_recording as module

    start = DailyWindow.from_date(date(2026, 11, 9)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    real_check, real_encode = daily.check_space, module.encode_daily_segment
    encoded = []

    async def encode(*args):
        result = await real_encode(*args)
        encoded.append(result)
        return result

    async def check(reserve=0, *, recording=False):
        if not recording and len(encoded) == 1:
            daily.settings.max_daily_storage_bytes = encoded[0]["size_bytes"] - 1
        return await real_check(reserve, recording=recording)

    try:
        await daily.tick(start)
        await service.store_frames(
            [FrameEnvelope("camera-main", start + i / 30, i, jpeg()) for i in range(301)]
        )
        monkeypatch.setattr(module, "encode_daily_segment", encode)
        monkeypatch.setattr(daily, "check_space", check)
        clock[0] = start + 10
        await daily.tick(clock[0])
        assert len(encoded) == 1
        assert not list((service.root / "daily").rglob("*.ts"))
        assert not service.db.execute(
            "SELECT 1 FROM daily_segments WHERE status='ready'"
        ).fetchone()
        assert service.db.execute("SELECT cursor FROM daily_cursors").fetchone()[0] == start
        daily._capacity_retry_at.clear()
        await daily.tick(clock[0])
        assert len(encoded) == 1, "Known size must reject admission before repeating expensive work"
        monkeypatch.setattr(daily, "check_space", real_check)
        daily.settings.max_daily_storage_bytes = 100_000_000
        daily._capacity_retry_at.clear()
        await daily.tick(clock[0])
        assert len(encoded) == 2
        assert (
            service.db.execute(
                "SELECT count(*) FROM daily_segments WHERE status='ready'"
            ).fetchone()[0]
            == 1
        )
        assert not service.db.execute(
            "SELECT 1 FROM daily_gaps WHERE reason='STORAGE_FULL'"
        ).fetchone()
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_capacity_recovery_compaction_ignores_cursor_lag_but_not_queue_pressure(
    tmp_path, monkeypatch
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    folder = service.root / "daily" / old["recording_id"]
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["export_state"] == "ready"
        service.queue = RecordingQueue(1200)
        service.daily_frame_watermarks["camera-main"] = clock[0] - 100
        daily.capacity_pending = {"camera-main": {"start": clock[0] - 100}}
        for i in range(61):
            service.queue.put_nowait(FrameEnvelope("camera-main", clock[0], i, b"jpeg"))
        await exports._compact(old["recording_id"])
        assert folder.exists(), "Real queued ingress still takes priority"
        while not service.queue.empty():
            frame = service.queue.get_nowait()
            service.queue.acknowledge(frame)
        await exports._compact(old["recording_id"])
        assert not folder.exists(), "Lag-only pressure cannot deadlock capacity recovery"
    finally:
        service.queue = None
        daily.capacity_pending = {}
        await exports.close()
        daily.initialized = False
        await daily.close()
        await service.close()
