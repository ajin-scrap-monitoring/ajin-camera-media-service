import asyncio
import importlib
import importlib.util
from datetime import date

import imageio_ffmpeg
import pytest
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope
from camera_media_service.recording import ApiError


def make_exports(daily):
    assert importlib.util.find_spec("camera_media_service.daily_export"), "Daily exporter missing"
    daily.settings.daily_export_cache_hours = 24
    daily.settings.max_daily_export_jobs = 8
    return importlib.import_module("camera_media_service.daily_export").DailyExportService(
        daily, daily.settings
    )


async def recorded_day(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at - 10]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(clock[0])
    raw = jpeg()
    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, raw))
    clock[0] = window.end_at
    await daily.tick(clock[0])
    items = await daily.list_days("camera-main", {}, 20, None)
    old = next(r for r in items if r["recording_date"] == "2026-10-14")
    return service, daily, clock, old


async def wait_ready(exports, daily, recording_id):
    for _ in range(200):
        day = await daily.get_day(recording_id)
        if day["export_state"] in ("ready", "failed"):
            return day
        await asyncio.sleep(0.01)
    pytest.fail("Export did not finish")


@pytest.mark.asyncio
async def test_finished_job_waiting_for_callback_does_not_block_deletion(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    exports.jobs[old["recording_id"]] = finished
    try:
        await exports.delete_day(old["recording_id"])
        assert (await daily.get_day(old["recording_id"]))["status"] == "deleted"
        assert not list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_export_is_single_idempotent_mp4_and_survives_restart(tmp_path, monkeypatch):
    service, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        results = await asyncio.gather(
            *[exports.request_export(day["recording_id"]) for _ in range(10)]
        )
        assert sum(created for _, created in results) == 0
        assert not exports.jobs
        await exports.maintenance(clock[0])
        assert len(exports.jobs) == 1
        ready = await wait_ready(exports, daily, day["recording_id"])
        assert ready["export_state"] == "ready", ready
        assert ready["size_bytes"] > 0
        response, created = await exports.request_export(day["recording_id"])
        assert not created and response["export_state"] == "ready"
        path = service.root / "daily-exports" / (day["recording_id"] + ".mp4")
        reader = imageio_ffmpeg.read_frames(str(path))
        metadata = next(reader)
        assert 0.9 <= metadata["duration"] <= 1.1
        assert sum(1 for _ in reader) >= 25
        await exports.close()
        await daily.close()
        await service.close()
        service, daily = make_daily(tmp_path, clock, monkeypatch)
        exports = make_exports(daily)
        assert (await daily.get_day(day["recording_id"]))["export_state"] == "ready"
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_export_failure_is_retryable_with_same_day_id(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    module = importlib.import_module("camera_media_service.daily_export")
    real = module.merge_daily_mp4

    async def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module, "merge_daily_mp4", fail)
    try:
        await exports.maintenance(clock[0])
        failed = await wait_ready(exports, daily, old["recording_id"])
        assert failed["export_state"] == "failed" and failed["coverage"] == old["coverage"]
        await asyncio.sleep(0)
        monkeypatch.setattr(module, "merge_daily_mp4", real)
        clock[0] += 31
        await exports.maintenance(clock[0])
        assert (await wait_ready(exports, daily, old["recording_id"]))["export_state"] == "ready"
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_today_none_and_insufficient_space_are_rejected(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        today = (await daily.list_days("camera-main", {}, 20, None))[0]
        with pytest.raises(ApiError) as error:
            await exports.request_export(today["recording_id"])
        assert error.value.code == "DAY_NOT_CLOSED"
        daily.settings.max_daily_storage_bytes = 1
        await exports.request_export(old["recording_id"])
        await exports.maintenance(clock[0])
        failed = await daily.get_day(old["recording_id"])
        assert failed["error"]["code"] == "INSUFFICIENT_STORAGE"
        assert not exports.jobs
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_active_day_cannot_be_deleted(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        today = (await daily.list_days("camera-main", {}, 20, None))[0]
        with pytest.raises(ApiError) as error:
            await exports.delete_day(today["recording_id"])
        assert error.value.code == "RECORDING_ACTIVE"
        await exports.maintenance(clock[0])
        await wait_ready(exports, daily, old["recording_id"])
        await exports.delete_day(old["recording_id"])
        await exports.delete_day(old["recording_id"])
        deleted = await daily.get_day(old["recording_id"])
        assert deleted["status"] == "deleted" and deleted["download_path"] is None
        assert not list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
        with pytest.raises(ApiError) as error:
            await exports.request_export(old["recording_id"])
        assert error.value.status == 410
    finally:
        await exports.close()
        await daily.close()
        await service.close()
