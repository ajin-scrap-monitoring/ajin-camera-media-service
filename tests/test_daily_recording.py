import asyncio
import importlib
import importlib.util
from datetime import date
from types import SimpleNamespace

import pytest

from camera_media_service.daily_catalog import DailyCatalog
from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub
from camera_media_service.recording import RecordingService, iso
from camera_media_service.store import LatestFrameStore


def jpeg():
    import io

    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (320, 240), "blue").save(raw, "JPEG")
    return raw.getvalue()


@pytest.mark.asyncio
async def test_normal_shutdown_flushes_short_tail(tmp_path, monkeypatch):
    import imageio_ffmpeg

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(clock[0])
        raw = jpeg()
        for i in range(10):
            await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, raw))
        clock[0] += 1
        await daily.close()
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert 0.9 <= day["recorded_seconds"] <= 1.1
        assert day["playback_state"] == "ready"
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_storage_limit_keeps_live_data_but_reports_recording_loss(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    daily.settings.max_daily_storage_bytes = 1
    try:
        await daily.tick(clock[0])
        raw = jpeg()
        for i in range(10):
            await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, raw))
        clock[0] += 10
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert day["recorded_seconds"] == 0
        assert any(g["reason_code"] == "STORAGE_FULL" for g in day["gaps"])
        assert service.db.execute("SELECT count(*) FROM frame_pins").fetchone()[0] == 0
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_export_reservations_protect_space_from_new_recording(tmp_path, monkeypatch):
    from camera_media_service.recording import ApiError

    clock = [DailyWindow.from_date(date(2026, 10, 14)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    daily.settings.max_daily_storage_bytes = 1000
    daily.storage_reservations = {"export": 900}
    with pytest.raises(ApiError, match="storage"):
        await daily.check_space(200)
    await daily.close()
    await service.close()


def make_daily(tmp_path, clock, monkeypatch):
    import camera_media_service.recording as recording

    monkeypatch.setattr(recording.time, "time", lambda: clock[0])
    assert importlib.util.find_spec("camera_media_service.daily_recording"), (
        "Daily recorder missing"
    )
    module = importlib.import_module("camera_media_service.daily_recording")
    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    settings = SimpleNamespace(
        daily_segment_seconds=10,
        max_daily_storage_bytes=100_000_000,
        min_free_storage_bytes=0,
        max_daily_pending_seconds=30,
        daily_width=320,
        daily_height=240,
        daily_fps=30,
    )
    daily = module.DailyRecordingService(
        service, DailyCatalog(service.db), clock=lambda: clock[0], settings=settings
    )
    return service, daily


@pytest.mark.asyncio
async def test_shutdown_allows_inflight_encoder_to_commit(tmp_path, monkeypatch):
    import imageio_ffmpeg

    import camera_media_service.daily_recording as module

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(clock[0])
    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, jpeg()))
    started, finish = asyncio.Event(), asyncio.Event()
    original = module.encode_daily_segment

    async def slow(*args):
        started.set()
        await finish.wait()
        return await original(*args)

    monkeypatch.setattr(module, "encode_daily_segment", slow)
    clock[0] += 10
    daily.task = asyncio.create_task(daily.tick(clock[0]))
    await started.wait()
    closing = asyncio.create_task(daily.close())
    await asyncio.sleep(0.01)
    finish.set()
    await closing
    day = (await daily.list_days("camera-main", {}, 20, None))[0]
    assert day["playback_state"] == "ready"
    assert len(daily.catalog.segments(day["recording_id"], ready_only=True)) == 1
    await service.close()


@pytest.mark.asyncio
async def test_offline_ticks_coalesce_loss_rows(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    daily.connection_changed("camera-main", False, clock[0])
    for i in range(1, 101):
        clock[0] = window.start_at + i / 4
        await daily.tick(clock[0])
    assert (
        service.db.execute(
            "SELECT count(*) FROM daily_gaps WHERE reason='CAMERA_DISCONNECTED'"
        ).fetchone()[0]
        == 1
    )
    await daily.close()
    await service.close()


@pytest.mark.asyncio
async def test_daily_profile_is_frozen_and_midnight_is_finalizing(tmp_path, monkeypatch):
    import imageio_ffmpeg

    import camera_media_service.daily_recording as module

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at - 20]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(clock[0])
    raw = jpeg()
    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, raw))
    clock[0] += 10
    await daily.tick(clock[0])
    day = (await daily.list_days("camera-main", {}, 20, None))[0]
    daily.settings.daily_width = 640
    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, raw))
    original = module.encode_daily_segment

    async def inspect(*args):
        assert (await daily.get_day(day["recording_id"]))["status"] == "finalizing"
        return await original(*args)

    monkeypatch.setattr(module, "encode_daily_segment", inspect)
    clock[0] = window.end_at
    await daily.tick(clock[0])
    segments = daily.catalog.segments(day["recording_id"], ready_only=True)
    assert len(segments) == 2 and segments[0]["profile"] == segments[1]["profile"]
    await daily.close()
    await service.close()


@pytest.mark.asyncio
async def test_recovery_reclaims_orphan_outer_slot_pins(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, jpeg()))
    with service.db:
        service.pin_frames("crashed-outer-slot", "camera-main", clock[0], clock[0] + 10)
    await service.close()
    clock[0] += 600
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    async with service.lock:
        with service.db:
            await service._prune(clock[0])
    assert service.db.execute("SELECT count(*) FROM frame_pins").fetchone()[0] == 0
    assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
    await daily.close()
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reenable", [True, False])
async def test_policy_effective_time_preserves_enabled_prefix(tmp_path, monkeypatch, reenable):
    import imageio_ffmpeg

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(clock[0])
    for i in range(100):
        await service.store_frame(FrameEnvelope("camera-main", window.start_at + i / 10, i, jpeg()))
    clock[0] += 3
    await daily.set_policy("camera-main", {"enabled": False, "retention_days": 30})
    if reenable:
        clock[0] = window.start_at + 7
        await daily.set_policy("camera-main", {"enabled": True, "retention_days": 30})
    clock[0] = window.start_at + 10
    await daily.tick(clock[0])
    day = (await daily.list_days("camera-main", {}, 20, None))[0]
    assert abs(day["recorded_seconds"] - (6 if reenable else 3)) < 0.04
    assert {
        "start_at": iso(window.start_at + 3),
        "end_at": iso(window.start_at + (7 if reenable else 10)),
        "reason_code": "RECORDING_DISABLED",
    } in day["gaps"]
    await daily.close()
    await service.close()


@pytest.mark.asyncio
async def test_shutdown_bounds_clock_jump_backlog(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    calls = []

    async def count(day, start, end):
        calls.append((start, end))
        if len(calls) > 5:
            raise RuntimeError("Unbounded shutdown backlog")

    monkeypatch.setattr(daily, "_record_slot", count)
    clock[0] += 86400
    await daily.close()
    assert len(calls) <= 4
    await service.close()


@pytest.mark.asyncio
async def test_empty_midnight_creates_one_day_each_and_never_marks_future_gap(
    tmp_path, monkeypatch
):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at - 5]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        first = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert first["status"] == "recording" and first["coverage"] is None
        assert all(g["end_at"] <= iso(clock[0]) for g in first["gaps"])
        clock[0] = window.end_at + 10
        await daily.tick(clock[0])
        items = await daily.list_days("camera-main", {}, 20, None)
        assert [i["recording_date"] for i in items] == ["2026-10-15", "2026-10-14"]
        assert items[1]["status"] == "closed" and items[1]["coverage"] == "none"
        assert items[0]["status"] == "recording" and items[0]["coverage"] is None
        assert all(g["end_at"] <= iso(clock[0]) for g in items[0]["gaps"])
        assert items[1]["recording_id"] == first["recording_id"]
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_real_segments_split_midnight_and_missing_middle(tmp_path, monkeypatch):
    import imageio_ffmpeg

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at - 10]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(clock[0])
        raw = jpeg()
        for i, t in enumerate(
            [window.end_at - 9 + j / 10 for j in range(10)]
            + [window.end_at - 3 + j / 10 for j in range(10)]
            + [window.end_at + j / 10 for j in range(10)]
        ):
            await service.store_frame(FrameEnvelope("camera-main", t, i, raw))
        clock[0] = window.end_at + 10
        await daily.tick(clock[0])
        items = await daily.list_days("camera-main", {}, 20, None)
        assert len(items) == 2 and items[1]["coverage"] == "partial"
        old_segments = daily.catalog.segments(items[1]["recording_id"], ready_only=True)
        new_segments = daily.catalog.segments(items[0]["recording_id"], ready_only=True)
        assert len(old_segments) == 2 and len(new_segments) == 1
        assert all(s["end_at"] <= window.end_at for s in old_segments)
        assert all(s["start_at"] >= window.end_at for s in new_segments)
        assert items[1]["recorded_seconds"] < 3
        assert any(
            g["start_at"] == iso(window.end_at - 8) and g["end_at"] == iso(window.end_at - 3)
            for g in items[1]["gaps"]
        )
        assert service.db.execute("SELECT count(*) FROM frame_pins").fetchone()[0] == 0
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", [True, False])
async def test_renamed_segment_is_recovered_without_reporting_unrecorded_tail(
    tmp_path, monkeypatch, crash
):
    import imageio_ffmpeg

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(clock[0])
    raw = jpeg()
    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, raw))
    clock[0] += 10
    await daily.tick(clock[0])
    day = (await daily.list_days("camera-main", {}, 20, None))[0]
    segment = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
    # The file has been renamed; simulate a crash before the ready DB result is committed.
    with service.db:
        body = dict(segment, status="processing", end_at=window.start_at + 10)
        service.db.execute(
            "UPDATE daily_segments SET status='processing',body=? WHERE id=?",
            (__import__("json").dumps(body), segment["segment_id"]),
        )
        service.db.execute("UPDATE daily_cursors SET cursor=?", (window.start_at,))
    if not crash:
        await daily.close()
    await service.close()
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(clock[0])
        recovered = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        assert len(daily.catalog.segments(day["recording_id"], ready_only=True)) == 1
        assert recovered["end_at"] < window.start_at + 2
        assert recovered["duration"] < 2
        assert service.db.execute("SELECT count(*) FROM frame_pins").fetchone()[0] == 0
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_service_interruption_materializes_missing_dates(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at + 10]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    await daily.close()
    await service.close()
    clock[0] = window.end_at + 86400 + 10
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        items = await daily.list_days("camera-main", {}, 20, None)
        assert [i["recording_date"] for i in items] == ["2026-10-16", "2026-10-15", "2026-10-14"]
        assert items[1]["coverage"] == "none"
        assert any(g["reason_code"] == "SERVICE_INTERRUPTED" for g in items[1]["gaps"])
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_policy_persists_and_does_not_change_current_retention(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        await daily.set_policy("camera-main", {"enabled": True, "retention_days": 7})
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert day["retention_days"] == 30
        assert (await daily.get_policy("camera-main"))["retention_days"] == 7
        clock[0] = window.end_at + 1
        await daily.tick(clock[0])
        assert (await daily.list_days("camera-main", {}, 20, None))[0]["retention_days"] == 7
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_explicit_short_loss_keeps_its_reason(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        daily.notify_loss("camera-main", clock[0] + 1, clock[0] + 1.2, "BUFFER_DROPPED")
        clock[0] += 10
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert any(
            g["reason_code"] == "BUFFER_DROPPED"
            and g["start_at"] == iso(window.start_at + 1)
            and g["end_at"] == iso(window.start_at + 1.2)
            for g in day["gaps"]
        )
    finally:
        await daily.close()
        await service.close()
