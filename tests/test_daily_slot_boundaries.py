from datetime import date

import imageio_ffmpeg
import pytest
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope


@pytest.mark.asyncio
@pytest.mark.parametrize("known_loss", [False, True])
async def test_slot_cut_uses_actual_adjacent_receipts_without_hiding_known_loss(
    tmp_path, monkeypatch, known_loss
):
    window = DailyWindow.from_date(date(2026, 10, 14))
    start = window.start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(start)
        raw = jpeg()
        for i in range(201):
            await service.store_frame(FrameEnvelope("camera-main", start + 0.03 + i / 10, i, raw))
        if known_loss:
            daily.notify_loss("camera-main", start + 9.98, start + 10.02, "BUFFER_DROPPED")
        clock[0] = start + 20.05
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        segments = daily.catalog.segments(day["recording_id"], ready_only=True)
        assert len(segments) == 2
        if known_loss:
            assert segments[0]["end_at"] <= start + 9.98
            assert segments[1]["start_at"] >= start + 10.02
            assert any(g["reason_code"] == "BUFFER_DROPPED" for g in day["gaps"])
        else:
            assert segments[0]["end_at"] == start + 10
            assert segments[1]["start_at"] == start + 10
            assert segments[1]["end_at"] == start + 20
            assert len(day["available_intervals"]) == 1
    finally:
        # Only the two complete slots are under test.
        daily.initialized = False
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_slot_boundary_does_not_hold_predecessor_over_real_source_gap(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 10, 14)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(start)
        raw = jpeg()
        receipts = [start + i / 10 for i in range(96)] + [start + 10.8 + i / 10 for i in range(101)]
        for i, at in enumerate(receipts):
            await service.store_frame(FrameEnvelope("camera-main", at, i, raw))
        clock[0] = start + 20.9
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        segments = daily.catalog.segments(day["recording_id"], ready_only=True)
        assert segments[0]["end_at"] == pytest.approx(start + 9.6)
        assert segments[1]["start_at"] == start + 10.8
        assert len(day["available_intervals"]) == 2
        assert any(g["reason_code"] == "UNKNOWN_CAPTURE_LOSS" for g in day["gaps"])
    finally:
        daily.initialized = False
        await daily.close()
        await service.close()
