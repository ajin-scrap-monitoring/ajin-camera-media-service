import importlib
from datetime import date

import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow, seoul_date
from camera_media_service.recording import ApiError, iso


async def setup(tmp_path, monkeypatch):
    now = DailyWindow.from_date(date(2026, 10, 26)).start_at + 3600
    clock = [now]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    module = importlib.import_module("camera_media_service.recorded_clips")
    clips = module.RecordedClipService(recording, daily)
    # Selection tests deliberately keep the real remux worker queued.
    await recording.encoding_slot.acquire()
    return recording, daily, clips, clock


def segment(daily, start, end, status="ready"):
    with daily.db:
        day = daily.catalog.ensure_day("camera-main", DailyWindow.from_date(seoul_date(start)), 30)
        item = daily.catalog.begin_segment(day["recording_id"], start, end)
        if status == "ready":
            path = daily.segment_path(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test-only-catalog-media")
            daily.catalog.complete_segment(
                item["segment_id"],
                {
                    "duration": end - start,
                    "size_bytes": path.stat().st_size,
                    "path": str(path.relative_to(daily.recording.root)),
                    "profile": {"width": 320, "height": 240, "fps": 30},
                },
            )
    return day, item


async def cleanup(recording, daily, clips):
    await clips.close()
    await daily.close()
    await recording.close()


@pytest.mark.asyncio
async def test_event_time_freezes_partial_past_and_future_and_duplicate(tmp_path, monkeypatch):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    segment(daily, now - 20, now)
    try:
        body, created = await clips.create_clip("camera-main", "event", iso(now - 5))
        assert created
        assert body["event_at"] == iso(now - 5)
        assert body["requested_start_at"] == iso(now - 35)
        assert body["requested_end_at"] == iso(now + 25)
        assert body["coverage"] == "partial"
        assert body["actual_start_at"] == iso(now - 20)
        assert body["actual_end_at"] == iso(now)
        assert [g["reason_code"] for g in body["gaps"]] == [
            "RECORDING_UNAVAILABLE",
            "FUTURE_NOT_RECORDED",
        ]
        assert recording.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 1
        clock[0] += 60
        await recording.set_policy(
            "camera-main", {"pre_roll_seconds": 1, "post_roll_seconds": 1, "retention_days": 1}
        )
        again, created = await clips.create_clip("camera-main", "event", iso(now - 1))
        assert not created
        assert again == body
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("missing", 404, "RECORDING_NOT_FOUND_AT_TIME"),
        ("future", 400, "EVENT_TIME_IN_FUTURE"),
        ("processing", 409, "RECORDING_NOT_READY"),
        ("failed", 404, "RECORDING_NOT_FOUND_AT_TIME"),
        ("file_missing", 404, "RECORDING_NOT_FOUND_AT_TIME"),
        ("gap", 404, "RECORDING_NOT_FOUND_AT_TIME"),
    ],
)
async def test_center_must_exist_not_just_neighboring_footage(
    tmp_path, monkeypatch, kind, status, code
):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    event_at = now - 10
    if kind == "future":
        event_at = now + 1
    elif kind == "processing":
        segment(daily, now - 20, now, "processing")
    elif kind == "failed":
        _, item = segment(daily, now - 20, now, "processing")
        with daily.db:
            daily.db.execute(
                "UPDATE daily_segments SET status='failed' WHERE id=?", (item["segment_id"],)
            )
    elif kind == "file_missing":
        _, item = segment(daily, now - 20, now)
        daily.segment_path(item).unlink()
    elif kind == "gap":
        segment(daily, now - 30, now)
        with daily.db:
            daily.db.execute(
                "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                ("camera-main", now - 11, now - 9, "CAMERA_DISCONNECTED"),
            )
    try:
        with pytest.raises(ApiError) as error:
            await clips.create_clip("camera-main", "event", iso(event_at))
        assert (error.value.status, error.value.code) == (status, code)
        assert recording.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_expiry_defers_physical_cleanup_until_clip_releases_source(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from camera_media_service.daily_export import DailyExportService

    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    day, item = segment(daily, now - 120, now - 60)
    with daily.db:
        day.update(status="closed", expires_at=iso(now + 1))
        daily.catalog.save_day(day)
    exports = DailyExportService(daily, SimpleNamespace(max_daily_export_jobs=8))
    source = daily.segment_path(item)
    try:
        await clips.create_clip("camera-main", "event", iso(now - 90))
        clock[0] += 2
        await exports.maintenance(clock[0])
        assert source.exists()
        await clips.close()
        await exports.maintenance(clock[0])
        assert not source.exists()
    finally:
        await exports.close()
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["queued", "inflight", "persisted_low_fps"])
async def test_received_unencoded_input_is_pending_even_before_writer_commit(
    tmp_path, monkeypatch, mode
):
    from camera_media_service.pipeline import FrameEnvelope

    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    at = now - 0.95
    if mode == "persisted_low_fps":
        for i, timestamp in enumerate([now - 1, now - 0.9]):
            await recording.store_frame(FrameEnvelope("camera-main", timestamp, i, b"jpeg"))
    else:
        recording.queue = recording.hub.subscribe(recording=True)
        clock[0] = now - 1
        recording.hub.publish("camera-main", b"jpeg")
        if mode == "inflight":
            # Removed from the asyncio queue but not acknowledged by the disk writer.
            recording.queue.get_nowait()
        clock[0] = now - 0.9
        recording.hub.publish("camera-main", b"jpeg")
        clock[0] = now
    try:
        with pytest.raises(ApiError) as error:
            await clips.create_clip("camera-main", "pending", iso(at))
        assert (error.value.status, error.value.code) == (409, "RECORDING_NOT_READY")
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, True, 123, "", "2026-10-26", "2026-10-26T01:00:00", "2026-02-30T00:00:00.000Z"]
)
async def test_event_timestamp_is_strict_utc(tmp_path, monkeypatch, value):
    recording, daily, clips, _ = await setup(tmp_path, monkeypatch)
    try:
        with pytest.raises(ApiError) as error:
            await clips.create_clip("camera-main", "event", value)
        assert error.value.status == 400
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_selection_crosses_midnight_and_reports_internal_gap(tmp_path, monkeypatch):
    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    midnight = DailyWindow.from_date(date(2026, 10, 26)).start_at
    clock[0] = midnight + 60
    segment(daily, midnight - 40, midnight - 10)
    segment(daily, midnight, midnight + 40)
    try:
        body, _ = await clips.create_clip("camera-main", "midnight", iso(midnight + 5))
        assert body["actual_start_at"] == iso(midnight - 25)
        assert body["actual_end_at"] == iso(midnight + 35)
        assert body["coverage"] == "partial"
        assert body["gaps"] == [
            {
                "start_at": iso(midnight - 10),
                "end_at": iso(midnight),
                "reason_code": "RECORDING_UNAVAILABLE",
            }
        ]
        assert recording.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 2
    finally:
        await cleanup(recording, daily, clips)


@pytest.mark.asyncio
async def test_original_day_cannot_be_deleted_during_queued_clip(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from camera_media_service.daily_export import DailyExportService

    recording, daily, clips, clock = await setup(tmp_path, monkeypatch)
    now = clock[0]
    day, _ = segment(daily, now - 120, now - 60)
    with daily.db:
        day["status"] = "closed"
        daily.catalog.save_day(day)
    exports = DailyExportService(daily, SimpleNamespace(max_daily_export_jobs=8))
    try:
        await clips.create_clip("camera-main", "event", iso(now - 90))
        with pytest.raises(ApiError) as error:
            await exports.delete_day(day["recording_id"])
        assert error.value.status == 409
        await clips.close()
        assert recording.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 0
        await exports.delete_day(day["recording_id"])
    finally:
        await exports.close()
        await cleanup(recording, daily, clips)
