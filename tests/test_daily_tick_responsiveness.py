import threading
from datetime import date

import imageio_ffmpeg
import pytest
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_wait", [False, True])
async def test_recording_tick_replays_day_snapshots_off_receiver_loop(
    tmp_path, monkeypatch, capacity_wait
):
    start = DailyWindow.from_date(date(2026, 11, 9)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    await daily.tick(start)
    await service.store_frames(
        [FrameEnvelope("camera-main", start + i / 30, i, jpeg()) for i in range(301)]
    )
    original = daily.catalog.decode_day
    main = threading.get_ident()
    reads = []

    def read(raw):
        assert threading.get_ident() != main, (
            "Recording tick replays cold snapshots on receiver loop"
        )
        reads.append(1)
        return original(raw)

    monkeypatch.setattr(daily.catalog, "decode_day", read)
    if capacity_wait:
        daily.settings.max_daily_storage_bytes = 1
    try:
        clock[0] = start + 10
        await daily.tick(clock[0])
        assert reads
        assert bool(daily.capacity_pending) is capacity_wait
    finally:
        monkeypatch.setattr(daily.catalog, "decode_day", original)
        await daily.close()
        await service.close()
