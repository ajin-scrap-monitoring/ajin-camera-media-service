import asyncio
import json
from datetime import UTC, datetime

import imageio_ffmpeg
import pytest
from test_media_info import make_video
from test_persisted_media_info import details_for

from camera_media_service import encoding, media_details
from camera_media_service.persisted_media_info import ensure_probe_table, file_identity


def parsed(value):
    assert value.endswith("Z")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_probe_time_survives_memory_reuse_and_restart_without_reprobe(tmp_path):
    path = make_video(tmp_path)
    details = details_for(tmp_path)
    before = datetime.now(UTC)
    first = await details.inspect(path)
    after = datetime.now(UTC)
    assert before <= parsed(first["measured_at"]) <= after
    assert first["metadata_origin"] == "request_probe"
    second = await details.inspect(path)
    assert second["metadata_origin"] == "memory_cache"
    assert second["measured_at"] == first["measured_at"]
    details.recording.db.close()
    restarted = details_for(tmp_path, ffmpeg="absent-after-restart")
    try:
        third = await restarted.inspect(path)
        assert third["metadata_origin"] == "persisted_probe"
        assert third["measured_at"] == first["measured_at"]
        assert third["source"] == "file_probe"
    finally:
        restarted.recording.db.close()


@pytest.mark.asyncio
async def test_legacy_measurement_has_null_time_on_every_reuse(tmp_path):
    path = make_video(tmp_path)
    info = await media_details.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    info.pop("measured_at", None)
    info.pop("metadata_origin", None)
    details = details_for(tmp_path, ffmpeg="absent-for-legacy")
    try:
        with details.recording.db:
            ensure_probe_table(details.recording.db)
            details.recording.db.execute(
                "INSERT INTO measured_media_files VALUES(?,?,?)",
                (path.name, json.dumps(file_identity(path.stat())), json.dumps(info)),
            )
        first = await details.inspect(path)
        assert first["measured_at"] is None
        assert first["metadata_origin"] == "persisted_probe"
        second = await details.inspect(path)
        assert second["measured_at"] is None
        assert second["metadata_origin"] == "memory_cache"
        assert second["codec"] == first["codec"]
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_unknown_information_never_claims_a_measurement_time(tmp_path):
    details = details_for(tmp_path)
    try:
        result = await details.inspect(tmp_path / "missing.mp4")
        assert result["status"] == "unknown"
        assert result["measured_at"] is None
        assert result["metadata_origin"] == "unknown"
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_generation_probe_stamps_actual_completion(tmp_path):
    path = make_video(tmp_path)
    before = datetime.now(UTC)
    result = await encoding.probe_media(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert before <= parsed(result["media_info"]["measured_at"]) <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_coalesced_probe_waiters_share_measurement_time_and_request_origin(
    tmp_path, monkeypatch
):
    path = make_video(tmp_path)
    details = details_for(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    original = media_details.inspect_media_info
    calls = []

    async def blocked(*args):
        calls.append(1)
        started.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(media_details, "inspect_media_info", blocked)
    first = asyncio.create_task(details.inspect(path))
    await started.wait()
    second = asyncio.create_task(details.inspect(path))
    try:
        for _ in range(100):
            if next(iter(details.flights.values()))["waiters"] == 2:
                break
            await asyncio.sleep(0.01)
        release.set()
        a, b = await asyncio.gather(first, second)
        assert len(calls) == 1
        assert a["metadata_origin"] == b["metadata_origin"] == "request_probe"
        assert a["measured_at"] == b["measured_at"]
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        details.recording.db.close()
