import asyncio

import imageio_ffmpeg
import pytest
from test_daily_archive import archive_ready
from test_daily_export import make_exports, recorded_day

from camera_media_service.recorded_clips import RecordedClipService
from camera_media_service.recording import iso


@pytest.mark.asyncio
async def test_event_clip_uses_canonical_mp4_after_original_ts_removed(tmp_path, monkeypatch):
    recording, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    clips = RecordedClipService(recording, daily)
    exports = make_exports(daily)
    try:
        original = daily.catalog.segments(old["recording_id"], ready_only=True)[0]
        await exports.maintenance(clock[0])
        ready = await archive_ready(exports, daily, old["recording_id"])
        assert ready["archive_state"] == "mp4", ready
        await exports.maintenance(clock[0])
        assert not list((recording.root / "daily" / old["recording_id"]).rglob("*.ts"))
        body, _ = await clips.create_clip(
            "camera-main", "archived", iso(original["start_at"] + 0.4)
        )
        for _ in range(500):
            result = await recording.get_clip(body["clip_id"])
            if result["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "ready", result
        assert result["actual_start_at"] == iso(original["start_at"])
        assert result["extraction_mode"] == "stream_copy"
        reader = imageio_ffmpeg.read_frames(str(recording.clips / (body["clip_id"] + ".mp4")))
        meta = next(reader)
        assert 0.9 <= meta["duration"] <= 1.1
        assert sum(1 for _ in reader) >= 25
    finally:
        await clips.close()
        await exports.close()
        await daily.close()
        await recording.close()
