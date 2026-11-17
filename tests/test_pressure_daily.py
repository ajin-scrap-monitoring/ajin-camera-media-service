import asyncio
import shutil

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_export_summary import owner

from camera_media_service.recording import ApiError


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mp4", "hls"])
async def test_status_is_bounded_and_http_stream_pin_lasts_until_completion(
    tmp_path, monkeypatch, kind
):
    import camera_media_service.daily_http_api as module

    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    key = day["recording_id"]
    path = exports._path(key)
    path.write_bytes(b"fake-mp4")
    day.update(archive_state="mp4", export_state="ready", size_bytes=path.stat().st_size)
    with recording.db:
        daily.catalog.save_day(day)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(request, file, **kwargs):
        try:
            entered.set()
            await release.wait()
            from aiohttp import web

            return web.Response(body=b"ok")
        finally:
            file.close()

    monkeypatch.setattr(module, "stream_file", slow)

    async def open_segment(*args):
        return path.open("rb"), path.stat().st_size

    monkeypatch.setattr(exports.playback, "open_segment", open_segment)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/v2/capacity-cleanup/status")
        assert response.status == 200
        status = await response.json()
        assert status["enabled"] is False and status["minimum_retention_guaranteed"] is False
        suffix = (
            "/content"
            if kind == "mp4"
            else "/media/" + daily.catalog.segments(key, ready_only=True)[0]["segment_id"] + ".ts"
        )
        task = asyncio.create_task(client.get("/api/v2/daily-recordings/" + key + suffix))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert recording.db.execute(
                "SELECT 1 FROM clip_recording_pins WHERE day_id=?", (key,)
            ).fetchone(), "download stream lost its source lease"
            with pytest.raises(ApiError) as error:
                await exports.delete_day(key)
            assert error.value.status == 409
        finally:
            release.set()
            await task
        assert not recording.db.execute(
            "SELECT 1 FROM clip_recording_pins WHERE day_id=?", (key,)
        ).fetchone()


@pytest.mark.asyncio
async def test_capacity_delete_revalidates_active_and_pinned_before_tombstone(
    tmp_path, monkeypatch
):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    try:
        key = day["recording_id"]
        assert hasattr(exports, "pressure_candidate"), "pressure candidate selection missing"
        with recording.db:
            day.update(status="recording")
            daily.catalog.save_day(day)
        with pytest.raises(ApiError):
            await exports.delete_day(key, reason="capacity_pressure", max_bytes=1000)
        assert (await daily.get_day(key))["status"] == "recording"
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.skipif(
    not shutil.rmtree.avoids_symlink_attacks, reason="Linux fd-safe pressure unlink"
)
@pytest.mark.asyncio
async def test_young_closed_archive_deleted_only_within_quota_and_catalog_reason(
    tmp_path, monkeypatch
):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    key = day["recording_id"]
    path = exports._path(key)
    path.write_bytes(b"fake-mp4")
    # Retain original segments to verify total owned bytes, not just the MP4 size.
    day.update(archive_state="mp4", export_state="ready", size_bytes=path.stat().st_size)
    with recording.db:
        daily.catalog.save_day(day)
    try:
        candidate, reason = await exports.pressure_candidate(1024**3)
        assert reason is None and candidate["recording_id"] == key and candidate["bytes"] >= 8
        with pytest.raises(ApiError):
            await exports.delete_day(key, reason="capacity_pressure", max_bytes=1)
        assert path.exists() and (await daily.get_day(key))["status"] == "closed"
        await exports.delete_day(key, reason="capacity_pressure", max_bytes=1024**3)
        state = daily.catalog.get_summary_state(key)
        assert state["status"] == "deleted" and state["deletion_reason"] == "capacity_pressure"
        assert state["cleanup_state"] == "complete" and not path.exists()
        with pytest.raises(ApiError) as error:
            await exports.request_export(key, summary=True)
        assert error.value.status == 410
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.skipif(
    not shutil.rmtree.avoids_symlink_attacks, reason="Linux fd-safe pressure unlink"
)
@pytest.mark.asyncio
async def test_capacity_partial_failure_keeps_tombstone_and_resumes_same_target(
    tmp_path, monkeypatch
):
    import camera_media_service.pressure_files as files

    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    key = day["recording_id"]
    path = exports._path(key)
    path.write_bytes(b"fake-mp4")
    day.update(archive_state="mp4", export_state="ready", size_bytes=8)
    with recording.db:
        daily.catalog.save_day(day)
    real = files.remove

    def partial(root, target, guard):
        (root / "daily-exports" / (target + ".mp4")).unlink(missing_ok=True)
        raise OSError("interrupted after unlink")

    monkeypatch.setattr(files, "remove", partial)
    try:
        with pytest.raises(OSError):
            await exports.delete_day(key, reason="capacity_pressure", max_bytes=1024**3)
        state = daily.catalog.get_summary_state(key)
        assert (
            state["status"] == "deleted"
            and state["cleanup_state"] == "failed"
            and state["deletion_reason"] == "capacity_pressure"
        )
        monkeypatch.setattr(files, "remove", real)
        await exports.delete_day(key, reason="capacity_pressure", max_bytes=1024**3)
        assert daily.catalog.get_summary_state(key)["cleanup_state"] == "complete"
        assert not (recording.root / "daily" / key).exists()
    finally:
        await exports.close()
        await daily.close()
        await recording.close()
