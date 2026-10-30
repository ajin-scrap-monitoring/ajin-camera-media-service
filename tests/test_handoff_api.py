import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_archive import archive_ready
from test_daily_export import make_exports, recorded_day

from camera_media_service.camera_status import CameraStatusTracker
from camera_media_service.http_api import create_http_app
from camera_media_service.recording import iso


@pytest.mark.asyncio
async def test_handoff_range_metadata_status_and_deletion_retry(tmp_path, monkeypatch):
    recording, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def no_background():
        pass

    monkeypatch.setattr(exports, "start", no_background)
    tracker = CameraStatusTracker(
        recording.cameras, clock=lambda: clock[0], monotonic=lambda: clock[0]
    )
    original = daily.catalog.segments(old["recording_id"], ready_only=True)[0]
    policy = await recording.get_policy("camera-main")
    async with TestClient(
        TestServer(create_http_app(recording, daily=daily, exports=exports, status_tracker=tracker))
    ) as client:
        response = await client.get("/api/v1/cameras/camera-main/status")
        assert response.status == 200
        assert (await response.json())["status"] == "unknown"
        assert response.headers["Cache-Control"] == "no-store"
        tracker.connected("camera-main", "current")
        tracker.frame("camera-main", "current", clock[0])
        assert (await (await client.get("/api/v1/cameras/camera-main/status")).json())[
            "status"
        ] == "healthy"
        clock[0] += 5
        assert (await (await client.get("/api/v1/cameras/camera-main/status")).json())[
            "status"
        ] == "interrupted"
        assert (await client.get("/api/v1/cameras/unknown/status")).status == 404
        payload = {
            "camera_id": "camera-main",
            "event_id": "range-handoff",
            "start_at": iso(original["start_at"] + 0.1),
            "end_at": iso(original["start_at"] + 0.7),
        }
        response = await client.post("/api/v1/clips", json=payload)
        assert response.status == 202, await response.text()
        created = await response.json()
        assert created["request_mode"] == "time_range"
        key = created["clip_id"]
        prefix = "/api/v1/clips/" + key
        for _ in range(500):
            detail = await (await client.get(prefix)).json()
            if detail["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.01)
        assert detail["status"] == "ready", detail
        assert detail["size_bytes"] > 0
        info = await (await client.get(prefix + "/media-info")).json()
        assert info["media_info"]["status"] == "ready", info
        assert info["media_info"]["codec"] == "h264"
        assert info["timestamp_info"]["capture_timestamp_supported"] is False
        assert info["thumbnail"]["status"] == "unsupported"
        content = await client.head(prefix + "/content")
        assert (
            content.status == 200 and int(content.headers["Content-Length"]) == detail["size_bytes"]
        )
        assert (
            await client.get(prefix + "/content", headers={"Range": "bytes=0-127"})
        ).status == 206
        assert (await client.post("/api/v1/clips", json=payload)).status == 200
        assert (
            await client.post(
                "/api/v1/clips", json={**payload, "end_at": iso(original["start_at"] + 0.8)}
            )
        ).status == 409
        assert await recording.get_policy("camera-main") == policy
        base = "/api/v2/daily-recordings/" + old["recording_id"]
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["export_state"] == "ready"
        await exports.maintenance(clock[0])
        day_info = await (await client.get(base + "/media-info")).json()
        assert day_info["file"]["status"] == "ready", day_info
        assert day_info["file"]["fps"] == 30
        archived = await client.post(
            "/api/v1/clips", json={**payload, "event_id": "archived-range"}
        )
        assert archived.status == 202, await archived.text()
        archived_key = (await archived.json())["clip_id"]
        for _ in range(500):
            archived_detail = await (await client.get("/api/v1/clips/" + archived_key)).json()
            if archived_detail["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.01)
        assert archived_detail["status"] == "ready", archived_detail
        assert archived_detail["extraction_mode"] == "stream_copy"
        remove = exports._remove_folder

        def fail(path):
            raise PermissionError("Injected storage access failure")

        monkeypatch.setattr(exports, "_remove_folder", fail)
        assert (await client.delete(base)).status == 503
        failed = await (await client.get(base)).json()
        assert failed["status"] == "deleted" and failed["cleanup_state"] == "failed"
        assert (await client.get(base + "/content")).status == 410
        monkeypatch.setattr(exports, "_remove_folder", remove)
        assert (await client.delete(base)).status == 204
        assert (await client.delete(base)).status == 204
        assert (await (await client.get(base)).json())["cleanup_state"] == "complete"
        assert (await client.get(base + "/media-info")).status == 410
        assert (await client.delete(prefix)).status == 204
        assert (await client.delete("/api/v1/clips/" + archived_key)).status == 204
        assert (await client.get(prefix + "/media-info")).status == 410


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"start_at": "2026-10-30T00:00:00.000Z"},
        {"retry_failed": True},
        {
            "event_at": "2026-10-30T00:00:00.000Z",
            "start_at": "2026-10-30T00:00:00.000Z",
            "end_at": "2026-10-30T00:01:00.000Z",
        },
    ],
)
async def test_range_http_rejects_ambiguous_or_incomplete_request(tmp_path, monkeypatch, extra):
    recording, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    async with TestClient(
        TestServer(create_http_app(recording, daily=daily, exports=exports))
    ) as client:
        response = await client.post(
            "/api/v1/clips", json={"camera_id": "camera-main", "event_id": "invalid", **extra}
        )
        assert response.status == 400
