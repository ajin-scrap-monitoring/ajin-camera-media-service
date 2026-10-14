import importlib
import importlib.util
from datetime import date

import pytest

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub
from camera_media_service.recording import RecordingService
from camera_media_service.store import LatestFrameStore


def make_catalog(service):
    assert importlib.util.find_spec("camera_media_service.daily_catalog"), "Daily catalog missing"
    return importlib.import_module("camera_media_service.daily_catalog").DailyCatalog(service.db)


@pytest.mark.asyncio
async def test_day_identity_and_event_policy_survive_restart(tmp_path):
    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    catalog = make_catalog(service)
    window = DailyWindow.from_date(date(2026, 10, 14))
    with service.db:
        day = catalog.ensure_day("camera-main", window, 30)
        assert catalog.ensure_day("camera-main", window, 30)["recording_id"] == day["recording_id"]
    await service.set_policy(
        "camera-main", {"pre_roll_seconds": 1, "post_roll_seconds": 2, "retention_days": 3}
    )
    await service.close()
    reopened = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    try:
        catalog = make_catalog(reopened)
        assert catalog.ensure_day("camera-main", window, 30)["recording_id"] == day["recording_id"]
        assert (await reopened.get_policy("camera-main"))["post_roll_seconds"] == 2
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_daily_pin_keeps_old_frames_until_release(tmp_path, monkeypatch):
    import camera_media_service.recording as module

    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    make_catalog(service)
    try:
        monkeypatch.setattr(module.time, "time", lambda: 1000.0)
        await service.store_frame(FrameEnvelope("camera-main", 1000.0, 1, b"jpeg"))
        async with service.lock:
            with service.db:
                assert len(service.pin_frames("daily-owner", "camera-main", 999.0, 1001.0)) == 1
        await service._prune(1500.0)
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 1
        service.release_frames("daily-owner")
        await service._prune(1500.0)
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_segment_records_are_not_playable_before_atomic_completion(tmp_path):
    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    catalog = make_catalog(service)
    try:
        day = catalog.ensure_day("camera-main", DailyWindow.from_date(date(2026, 10, 14)), 30)
        segment = catalog.begin_segment(day["recording_id"], 1791903600.0, 1791903610.0)
        assert catalog.segments(day["recording_id"], ready_only=True) == []
        catalog.complete_segment(
            segment["segment_id"],
            {"duration": 10.0, "size_bytes": 100, "profile": {}, "path": "generated.ts"},
        )
        assert len(catalog.segments(day["recording_id"], ready_only=True)) == 1
    finally:
        await service.close()
