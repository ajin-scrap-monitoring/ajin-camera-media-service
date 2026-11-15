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


@pytest.mark.asyncio
async def test_summary_pages_large_legacy_guard_and_cursor_binding(tmp_path, monkeypatch):
    from camera_media_service import daily_http_api

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    key = old["recording_id"]
    day = daily.catalog.get_day(key)
    expected = [
        {"start_at": str(i), "end_at": str(i + 1), "reason_code": "STORAGE_FULL"}
        for i in range(300)
    ]
    day.update(gaps=expected, segments=[{"path": "/private"}], internal_path="/private")
    with service.db:
        daily.catalog.save_day(day)
    monkeypatch.setattr(daily_http_api, "RESPONSE_BYTES", 4096, raising=False)
    async with TestClient(
        TestServer(http_api.create_http_app(service, daily=daily, exports=exports))
    ) as client:
        base = "/api/v2/daily-recordings/" + key
        result = await client.get(base)
        assert result.status == 503
        assert (await result.json())["error"]["code"] == "DAILY_RESPONSE_TOO_LARGE"
        summary = await (await client.get(base + "/summary")).json()
        assert summary["recording_id"] == key
        assert not {"gaps", "available_intervals", "segments", "internal_path"} & summary.keys()
        listing = await client.get("/api/v2/cameras/camera-main/daily-recordings/summary?limit=1")
        assert listing.status == 200 and (await listing.json())["next_cursor"]
        page = await (await client.get(base + "/gaps?limit=25")).json()
        assert page["total"] == 300 and len(page["items"]) == 25
        cursor = page["next_cursor"]
        assert (
            await client.get(base + "/gaps", params={"limit": 26, "cursor": cursor})
        ).status == 400
        assert (
            await client.get(base + "/available-intervals", params={"limit": 25, "cursor": cursor})
        ).status == 400
        assert (await client.get(base + "/gaps?limit=501")).status == 400
        actual = page["items"]
        while page["next_cursor"]:
            response = await client.get(
                base + "/gaps", params={"limit": 25, "cursor": page["next_cursor"]}
            )
            assert response.status == 200
            page = await response.json()
            actual.extend(page["items"])
        assert actual == expected
        with service.db:
            service.db.execute(
                "UPDATE daily_revisions SET revision=revision+1 WHERE day_id=?", (key,)
            )
            if not service.db.execute(
                "SELECT 1 FROM daily_revisions WHERE day_id=?", (key,)
            ).fetchone():
                service.db.execute("INSERT INTO daily_revisions VALUES(?,1000)", (key,))
        stale = await client.get(base + "/gaps", params={"limit": 25, "cursor": cursor})
        assert stale.status == 409
        clock[0] += 3601
        assert (
            await client.get(base + "/gaps", params={"limit": 25, "cursor": cursor})
        ).status == 400


@pytest.mark.asyncio
async def test_archive_status_playback_and_cleanup_need_no_timeline_decode(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    key = old["recording_id"]
    await exports._enqueue(key)
    await wait_ready(exports, daily, key)

    def fail(*args, **kwargs):
        raise AssertionError("scalar operation must not decode historical timelines")

    monkeypatch.setattr(daily.catalog, "get_day", fail)
    await exports.request_export(key)
    day, segments = await daily.playback_snapshot(key)
    assert day["recording_id"] == key and segments
    await exports.delete_day(key)
    assert daily.catalog.get_state(key)["cleanup_state"] == "complete"
    daily.initialized = False
    await exports.close()
    await daily.close()
    await service.close()


@pytest.mark.asyncio
async def test_retention_status_is_read_only_and_reports_missing_full_day_sample(
    tmp_path, monkeypatch
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def idle_maintenance(now):
        pass

    monkeypatch.setattr(exports, "maintenance", idle_maintenance)
    before = daily.catalog.get_state(old["recording_id"])
    async with TestClient(
        TestServer(http_api.create_http_app(service, daily=daily, exports=exports))
    ) as client:
        response = await client.get("/api/v2/recording-retention-status")
        assert response.status == 200
        body = await response.json()
        assert body["status"] == "unknown" and body["policy_changed"] is False
        assert body["estimated_retention_bytes"] is None
        assert body["cameras"][0]["retention_days"] == 30
        assert body["recording_capacity_pending"] == []
        assert daily.catalog.get_state(old["recording_id"]) == before
        assert (await client.get("/api/v2/recording-retention-status?delete=true")).status == 400
