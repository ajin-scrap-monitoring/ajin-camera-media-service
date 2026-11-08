import asyncio
import io
import threading
from datetime import date
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow, StoredFrame
from camera_media_service.pipeline import FrameEnvelope


@pytest.mark.asyncio
async def test_daily_cursor_advances_while_optional_metadata_commit_is_blocked(
    tmp_path, monkeypatch
):
    import camera_media_service.persisted_media_info as module

    start = DailyWindow.from_date(date(2026, 11, 8)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    daily.settings.daily_fps = 15
    await daily.tick(clock[0])
    entered, release = threading.Event(), threading.Event()
    original = module._optional_write

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_optional_write", blocked)
    raw = jpeg()
    tick = None
    try:
        for slot in range(2):
            await service.store_frames(
                [
                    FrameEnvelope("camera-main", start + slot * 10 + i / 30, i, raw)
                    for i in range(300)
                ]
            )
            clock[0] = start + (slot + 1) * 10
            tick = asyncio.create_task(daily.tick(clock[0]))
            if slot == 0:
                assert await asyncio.to_thread(entered.wait, 5)
            await asyncio.wait_for(asyncio.shield(tick), 3)
            cursor = service.db.execute(
                "SELECT cursor FROM daily_cursors WHERE camera='camera-main'"
            ).fetchone()[0]
            assert cursor == start + (slot + 1) * 10
            assert not release.is_set()
        # Daily selection must not thin the retained source used by events.
        async with service.lock:
            async with service.transaction():
                original_frames = service.pin_frames(
                    "event-fixture", "camera-main", start, start + 20
                )
                assert len(original_frames) == 600
                service.release_frames("event-fixture")
        assert (
            service.db.execute(
                "SELECT count(*) FROM daily_segments WHERE status='ready'"
            ).fetchone()[0]
            == 2
        )
    finally:
        release.set()
        if tick is not None:
            await asyncio.gather(tick, return_exceptions=True)
        await daily.close()
        await service.close()


def test_daily_selector_uses_receipt_time_grid_without_changing_source_timestamps():
    from camera_media_service.daily_sampling import select_daily_frames

    start = 1794092400.0
    frames = [StoredFrame(f"{i}.jpg", start + i / 30, 100) for i in range(300)]
    selected = select_daily_frames(frames, 15, start + 10)
    assert len(selected) == 150
    assert selected == frames[::2]
    assert all(frame in frames for frame in selected)


def test_daily_selector_preserves_sparse_input_and_small_partial_slot():
    from camera_media_service.daily_sampling import select_daily_frames

    frames = [StoredFrame(f"{i}.jpg", 1000 + i / 5, 100) for i in range(5)]
    assert select_daily_frames(frames, 15, 1001) == frames
    assert select_daily_frames(frames[:1], 15, 1000.001) == frames[:1]
    assert select_daily_frames([], 15, 1001) == []


def test_daily_selector_uses_timestamps_for_jittered_receipts():
    from camera_media_service.daily_sampling import select_daily_frames

    times = [0, 0.01, 0.041, 0.071, 0.099, 0.14, 0.195, 0.201, 0.27]
    frames = [StoredFrame(f"{i}.jpg", 1000 + t, 100) for i, t in enumerate(times)]
    assert select_daily_frames(frames, 15, 1000.29) == [frames[i] for i in (0, 2, 4, 6, 7)]


@pytest.mark.asyncio
async def test_daily_encoder_halves_decoder_inputs_but_preserves_original_tail(
    tmp_path, monkeypatch
):
    import camera_media_service.encoding as module

    buffer = tmp_path / "buffer"
    buffer.mkdir()
    frames = []
    for i in range(60):
        image = io.BytesIO()
        Image.new("RGB", (64, 48), (i * 4, 20, 20)).save(image, "JPEG")
        path = buffer / f"{i}.jpg"
        path.write_bytes(image.getvalue())
        frames.append(StoredFrame(path.name, 1000 + i / 30, path.stat().st_size))
    manifests = []
    original = module.start_media_process

    async def capture(*args, **kwargs):
        if "concat" in args:
            manifest = Path(args[args.index("-i") + 1])
            manifests.append(manifest.read_text(encoding="utf-8"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "start_media_process", capture)
    metadata = await module.encode_daily_segment(
        imageio_ffmpeg.get_ffmpeg_exe(),
        buffer,
        tmp_path / "daily.ts",
        frames,
        {"width": 64, "height": 48, "fps": 15},
    )
    names = [line for line in manifests[0].splitlines() if line.startswith("file ")]
    assert len(names) == 31  # 30 chosen JPEGs plus the existing concat end marker.
    assert names[:-1] == [f"file '{i}.jpg'" for i in range(0, 60, 2)]
    assert metadata["start_at"] == 1000
    assert metadata["end_at"] == pytest.approx(1002, abs=1e-7)
    assert metadata["duration"] == pytest.approx(2, abs=0.002)
    assert metadata["media_info"]["fps"] == 15
    assert len(list(buffer.glob("*.jpg"))) == 60


@pytest.mark.asyncio
async def test_daily_sampling_does_not_fill_real_source_gap(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 8)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    daily.settings.daily_fps = 15
    try:
        await daily.tick(clock[0])
        raw = jpeg()
        for offset in (0, 5):
            await service.store_frames(
                [FrameEnvelope("camera-main", start + offset + i / 30, i, raw) for i in range(30)]
            )
        clock[0] = start + 10
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        parts = daily.catalog.segments(day["recording_id"], ready_only=True)
        assert len(parts) == 2
        assert parts[0]["end_at"] == pytest.approx(start + 1, abs=1e-5)
        assert parts[1]["start_at"] == start + 5
        assert day["has_gaps"]
        assert any(g["reason_code"] == "UNKNOWN_CAPTURE_LOSS" for g in day["gaps"])
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 60
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_daily_cache_pressure_uses_recording_clock(tmp_path, monkeypatch):
    from camera_media_service.pipeline import RecordingQueue

    start = DailyWindow.from_date(date(2026, 11, 8)).start_at
    clock = [start]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(clock[0])
        service.queue = RecordingQueue(1200)
        await service.store_frames(
            [FrameEnvelope("camera-main", start + i / 30, i, jpeg()) for i in range(30)]
        )
        clock[0] += 10
        await daily.tick(clock[0])
        await asyncio.wait_for(daily.metadata_writer.join(), 1)
        assert (tmp_path / "media-info.sqlite3").exists()
    finally:
        service.queue = None
        await daily.close()
        await service.close()
