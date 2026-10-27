import importlib.util

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_export import make_exports, recorded_day, wait_ready

from camera_media_service import http_api


@pytest.mark.asyncio
async def test_daily_http_json_hls_mp4_range_and_delete(tmp_path, monkeypatch):
    assert importlib.util.find_spec("camera_media_service.daily_http_api"), "Daily routes missing"
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    async with TestClient(
        TestServer(http_api.create_http_app(service, daily=daily, exports=exports))
    ) as client:
        base = "/api/v2/daily-recordings/" + old["recording_id"]
        listing = await (await client.get("/api/v2/cameras/camera-main/daily-recordings")).json()
        assert len(listing["items"]) == 2
        detail = await (await client.get(base)).json()
        assert detail["recording_date"] == "2026-10-14" and detail["coverage"] == "partial"
        playlist = await client.get(base + "/hls/index.m3u8")
        text = await playlist.text()
        assert playlist.status == 200 and "#EXT-X-ENDLIST" in text
        segment_path = next(line for line in text.splitlines() if line.startswith("/"))
        chunk = await client.get(segment_path, headers={"Range": "bytes=0-31"})
        assert chunk.status == 206 and len(await chunk.read()) == 32
        assert (await client.head(segment_path)).status == 200
        assert (await client.get(base + "/content")).status in (200, 409)
        assert (await client.post(base + "/exports")).status in (200, 202)
        await wait_ready(exports, daily, old["recording_id"])
        assert (await client.post(base + "/exports")).status == 200
        response = await client.get(
            base + "/content?download=true", headers={"Range": "bytes=0-31"}
        )
        assert response.status == 206 and len(await response.read()) == 32
        assert "attachment" in response.headers["Content-Disposition"]
        assert "2026-10-14" in response.headers["Content-Disposition"]
        assert (await client.head(base + "/content")).status == 200
        assert (await client.delete(base)).status == 204
        assert (await client.delete(base)).status == 204
        assert (await client.get(base + "/content")).status == 410


@pytest.mark.asyncio
async def test_daily_date_filters_cursor_policy_and_invalid_request(tmp_path, monkeypatch):
    assert importlib.util.find_spec("camera_media_service.daily_http_api"), "Daily routes missing"
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    async with TestClient(
        TestServer(http_api.create_http_app(service, daily=daily, exports=exports))
    ) as client:
        path = "/api/v2/cameras/camera-main/daily-recordings"
        first = await (await client.get(path + "?limit=1")).json()
        second = await (
            await client.get(path, params={"limit": "1", "cursor": first["next_cursor"]})
        ).json()
        assert len(second["items"]) == 1 and second["items"][0]["recording_date"] == "2026-10-14"
        assert (
            await client.get(path, params={"limit": "2", "cursor": first["next_cursor"]})
        ).status == 400
        only = await (await client.get(path + "?from_date=2026-10-14&to_date=2026-10-15")).json()
        assert len(only["items"]) == 1
        assert (await client.get(path + "?from_date=2026-02-30")).status == 400
        assert (await client.get(path + "?limit=101")).status == 400
        assert (await client.get(path.replace("camera-main", "missing"))).status == 404
        policy = "/api/v2/cameras/camera-main/daily-recording-policy"
        assert (await client.get(policy)).status == 200
        assert (await client.put(policy, json={"enabled": True, "retention_days": 7})).status == 200
        assert (
            await client.put(
                policy, json={"enabled": True, "retention_days": 7, "pre_roll_seconds": 10}
            )
        ).status == 400
        today = first["items"][0]
        assert (
            await client.post("/api/v2/daily-recordings/" + today["recording_id"] + "/exports")
        ).status == 409
