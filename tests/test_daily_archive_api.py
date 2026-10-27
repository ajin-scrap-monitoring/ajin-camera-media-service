import asyncio

import imageio_ffmpeg
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_archive import archive_ready
from test_daily_export import make_exports, recorded_day

from camera_media_service.http_api import create_http_app
from camera_media_service.recording import iso


@pytest.mark.asyncio
async def test_retained_ts_remains_playable_while_missing_archive_is_rebuilt(tmp_path, monkeypatch):
    recording, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def no_background():
        pass

    monkeypatch.setattr(exports, "start", no_background)
    key = old["recording_id"]
    original = daily.catalog.segments(key, ready_only=True)[0]
    with recording.db:
        recording.db.execute("INSERT INTO clip_recording_pins VALUES(?,?)", ("hold", key))
    async with TestClient(
        TestServer(create_http_app(recording, daily=daily, exports=exports))
    ) as client:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["export_state"] == "ready"
        exports._path(key).unlink()
        with recording.db:
            recording.db.execute("DELETE FROM clip_recording_pins WHERE clip_id='hold'")
        await exports.maintenance(clock[0])
        response = await client.get(
            f"/api/v2/daily-recordings/{key}/media/{original['segment_id']}.ts"
        )
        assert response.status == 200, await response.text()
        assert len(await response.read()) == original["size_bytes"]
        assert (await daily.get_day(key))["export_state"] == "failed"


@pytest.mark.asyncio
async def test_http_status_never_starts_archive_and_hls_survives_source_compaction(
    tmp_path, monkeypatch
):
    recording, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def no_background():
        pass

    monkeypatch.setattr(exports, "start", no_background)
    original = daily.catalog.segments(old["recording_id"], ready_only=True)[0]
    async with TestClient(
        TestServer(create_http_app(recording, daily=daily, exports=exports))
    ) as client:
        base = "/api/v2/daily-recordings/" + old["recording_id"]
        assert (await client.post(base + "/exports")).status == 202
        assert (await client.get(base + "/content")).status == 409
        assert not exports.jobs and not list(exports.output.glob("*.mp4"))
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["archive_state"] == "mp4"
        await exports.maintenance(clock[0])
        assert not list((recording.root / "daily" / old["recording_id"]).rglob("*.ts"))
        canonical = exports.output / (old["recording_id"] + ".mp4")
        modified = canonical.stat().st_mtime_ns
        assert (await client.post(base + "/exports")).status == 200
        assert not exports.jobs and canonical.stat().st_mtime_ns == modified
        playlist = await (await client.get(base + "/hls/index.m3u8")).text()
        path = next(p for p in playlist.splitlines() if p.startswith("/api/"))
        response = await client.get(path)
        assert response.status == 200, await response.text()
        saved = tmp_path / "hls.ts"
        saved.write_bytes(await response.read())
        reader = imageio_ffmpeg.read_frames(str(saved))
        meta = next(reader)
        assert 0.9 <= meta["duration"] <= 1.1
        assert sum(1 for _ in reader) >= 25
        response = await client.get(path, headers={"Range": "bytes=0-187"})
        assert response.status == 206 and len(await response.read()) == 188
        assert (await client.head(path)).status == 200
        response = await client.get(base + "/content", headers={"Range": "bytes=0-127"})
        assert response.status == 206 and len(await response.read()) == 128
        response = await client.post(
            "/api/v1/clips",
            json={
                "camera_id": "camera-main",
                "event_id": "from-archive",
                "event_at": iso(original["start_at"] + 0.4),
            },
        )
        assert response.status == 202, await response.text()
        clip = await response.json()
        for _ in range(500):
            detail = await (await client.get("/api/v1/clips/" + clip["clip_id"])).json()
            if detail["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.01)
        assert detail["status"] == "ready", detail
        assert (await client.delete(base)).status == 204
        assert not canonical.exists()
        assert (await client.get(path)).status == 410
        assert (await client.get("/api/v1/clips/" + clip["clip_id"] + "/content")).status == 200
