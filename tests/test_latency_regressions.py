import threading
import time
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_recent_loss_merge_does_not_scan_all_historical_losses(tmp_path, monkeypatch):
    from datetime import date

    from test_daily_recording import make_daily

    from camera_media_service.daily_models import DailyWindow

    # A VM instruction budget catches the historical scan without wall-clock limits.
    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    calls = 0

    def budget():
        nonlocal calls
        calls += 1
        return int(calls > 50)

    try:
        with recording.db:
            recording.db.executemany(
                "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                [("camera-main", i * 2, i * 2 + 1, "BUFFER_DROPPED") for i in range(20000)],
            )
        recording.db.set_progress_handler(budget, 100)
        try:
            with recording.db:
                daily._save_loss("camera-main", clock[0], clock[0] + 1, "BUFFER_DROPPED")
                daily._save_loss("camera-main", clock[0] + 1, clock[0] + 2, "BUFFER_DROPPED")
        finally:
            recording.db.set_progress_handler(None, 0)
        assert calls <= 50
        row = recording.db.execute(
            "SELECT start,end FROM daily_gaps ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == (clock[0], clock[0] + 2)
    finally:
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_slow_daily_metadata_path_resolution_keeps_event_loop_responsive(
    tmp_path, monkeypatch
):
    from test_daily_export import make_exports, recorded_day

    from camera_media_service.media_details import MediaDetails

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    owner = threading.get_ident()
    original = Path.resolve
    slow_calls_on_owner = []

    def slow(path, *args, **kwargs):
        if path.suffix == ".ts":
            if threading.get_ident() == owner:
                slow_calls_on_owner.append(str(path))
            time.sleep(0.04)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", slow)
    try:
        info = await MediaDetails(recording, daily, exports).day(day["recording_id"])
        assert info["segments"]["measured"] >= 1
        assert not slow_calls_on_owner
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_buffer_budget_does_not_restat_every_catalogued_jpeg(tmp_path, monkeypatch):
    from camera_media_service.pipeline import LiveFrameHub
    from camera_media_service.recording import RecordingService
    from camera_media_service.store import LatestFrameStore

    recording = RecordingService(tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()))
    for i in range(200):
        (recording.buffer / f"{i}.jpg").write_bytes(b"x" * 100)
    with recording.db:
        recording.db.executemany(
            "INSERT INTO frames VALUES(?,?,?,?)", [(f"{i}.jpg", "cam", i, 100) for i in range(200)]
        )
    recording.max_storage_bytes = 10**9
    stats = []
    original = Path.stat

    def observed(path, *args, **kwargs):
        if path.suffix == ".jpg":
            stats.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", observed)
    try:
        await recording._check_buffer_capacity(0)
        assert not stats
        recording.max_storage_bytes = 19999
        with pytest.raises(OSError, match="total storage budget"):
            await recording._check_buffer_capacity(0)
    finally:
        await recording.close()
