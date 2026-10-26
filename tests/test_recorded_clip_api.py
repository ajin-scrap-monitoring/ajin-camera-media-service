import asyncio
from datetime import date

import imageio_ffmpeg
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_clip_media import make_ts
from test_daily_recording import make_daily
from test_recorded_clips import segment

from camera_media_service.daily_models import DailyWindow
from camera_media_service.http_api import create_http_app
from camera_media_service.pipeline import LiveFrameHub
from camera_media_service.recording import RecordingService, iso
from camera_media_service.store import LatestFrameStore


@pytest.mark.asyncio
async def test_explicit_event_requires_recording_and_validates_timestamp(tmp_path):
    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    async with TestClient(TestServer(create_http_app(service))) as client:
        response = await client.post(
            "/api/v1/clips",
            json={
                "camera_id": "camera-main",
                "event_id": "disabled",
                "event_at": "2026-10-25T00:00:00.000Z",
            },
        )
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "RECORDING_NOT_ENABLED"
        for value in [None, True, "2026-10-25T00:00:00"]:
            response = await client.post(
                "/api/v1/clips",
                json={"camera_id": "camera-main", "event_id": "invalid", "event_at": value},
            )
            assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_offset", "coverage"), [(-2.65, "complete"), (-5, "partial"), (-0.65, "partial")]
)
async def test_recorded_clip_http_download_range_delete_and_restart(
    tmp_path, monkeypatch, event_offset, coverage
):
    # Use real TS packets; no JPEG frames or event-time encoder is available.
    now = DailyWindow.from_date(date(2026, 10, 26)).start_at + 3600
    clock = [now]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    day, item = segment(daily, now - 6, now)
    original = make_ts(tmp_path)
    destination = daily.segment_path(item)
    destination.write_bytes(original.read_bytes())
    with service.db:
        daily.catalog.complete_segment(
            item["segment_id"],
            {
                "duration": 6.0,
                "size_bytes": destination.stat().st_size,
                "path": str(destination.relative_to(tmp_path)),
                "profile": {"width": 160, "height": 120, "fps": 10},
            },
        )
    await service.set_policy(
        "camera-main", {"pre_roll_seconds": 2, "post_roll_seconds": 2, "retention_days": 30}
    )

    # Preserve test clock/catalog while the real HTTP lifecycle starts/stops.
    async def no_tick():
        return None

    monkeypatch.setattr(daily, "start", no_tick)
    monkeypatch.setattr(daily, "close", no_tick)
    from types import SimpleNamespace

    from camera_media_service.daily_export import DailyExportService

    exports = DailyExportService(daily, SimpleNamespace(max_daily_export_jobs=8))
    payload = {
        "camera_id": "camera-main",
        "event_id": "recorded",
        "event_at": iso(now + event_offset),
    }
    async with TestClient(
        TestServer(create_http_app(service, daily=daily, exports=exports))
    ) as client:
        response = await client.post("/api/v1/clips", json=payload)
        assert response.status == 202, await response.text()
        body = await response.json()
        path = response.headers["Location"]
        assert body["event_at"] == payload["event_at"]
        for _ in range(200):
            response = await client.get(path)
            detail = await response.json()
            if detail["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.02)
        assert detail["status"] == "ready", detail
        assert detail["coverage"] == coverage
        assert detail["extraction_mode"] == "stream_copy"
        response = await client.get(path + "/content", headers={"Range": "bytes=0-127"})
        assert response.status == 206
        assert len(await response.read()) == 128
        assert (await client.post("/api/v1/clips", json=payload)).status == 200
        assert service.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 0
        clip_id = detail["clip_id"]
    reopened = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    try:
        assert (await reopened.get_clip(clip_id))["status"] == "ready"
        await reopened.delete_clip(clip_id)
        await reopened.delete_clip(clip_id)
        assert (await reopened.get_clip(clip_id))["status"] == "deleted"
        assert not (reopened.clips / (clip_id + ".mp4")).exists()
    finally:
        await reopened.close()
