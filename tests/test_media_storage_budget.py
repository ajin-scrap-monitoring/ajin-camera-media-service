from datetime import date

import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub
from camera_media_service.recording import ApiError, RecordingService
from camera_media_service.storage import LocalStorageBackend
from camera_media_service.store import LatestFrameStore


@pytest.mark.asyncio
async def test_total_budget_counts_clips_buffer_and_catalog(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    (service.clips / "retained.mp4").write_bytes(b"x" * 1000)
    daily.settings.max_media_storage_bytes = (
        sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file()) - 1
    )
    try:
        with pytest.raises(ApiError) as error:
            await daily.check_space()
        assert error.value.code == "INSUFFICIENT_STORAGE"
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_recorder_leaves_room_for_archive_but_archive_can_use_it(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    folder = tmp_path / "daily"
    folder.mkdir(exist_ok=True)
    (folder / "source.ts").write_bytes(b"x" * 600)
    daily.settings.max_daily_storage_bytes = 1000
    daily.settings.daily_archive_workspace_bytes = 400
    try:
        with pytest.raises(ApiError) as error:
            await daily.check_space(1, recording=True)
        assert error.value.code == "INSUFFICIENT_STORAGE"
        await daily.check_space(400)
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_missing_storage_guard_rejects_before_directory_creation(tmp_path):
    target = tmp_path / "missing-media"

    def guard():
        raise OSError("SD unavailable")

    with pytest.raises(OSError, match="SD unavailable"):
        RecordingService(target, {"cam"}, LiveFrameHub(LatestFrameStore()), storage_guard=guard)
    assert not target.exists()


@pytest.mark.asyncio
async def test_guarded_write_never_falls_back_after_device_loss(tmp_path):
    unavailable = [False]

    def guard():
        if unavailable[0]:
            raise OSError("SD removed")

    service = RecordingService(
        tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()), storage_guard=guard
    )
    try:
        unavailable[0] = True
        with pytest.raises(OSError, match="SD removed"):
            await service.store_frame(FrameEnvelope("cam", 1, 1, b"\xff\xd8\xff\xd9"))
        assert not list(service.buffer.glob("*.jpg"))
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
    finally:
        unavailable[0] = False
        await service.close()


@pytest.mark.asyncio
async def test_legacy_segment_writer_checks_device_before_mkdir(tmp_path):
    def guard():
        raise OSError("SD removed")

    backend = LocalStorageBackend(str(tmp_path), storage_guard=guard)
    with pytest.raises(OSError, match="SD removed"):
        await backend.write_frame(FrameEnvelope("cam", 1, 1, b"jpeg"))
    assert not (tmp_path / "cam").exists()


@pytest.mark.asyncio
async def test_total_budget_credits_only_counted_clip_output_bytes(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.max_buffer_bytes = 0
    path = service.clips / "job.tmp.mp4"
    path.write_bytes(b"x" * 300)
    daily.storage_reservations["job"] = 400
    daily.storage_reservation_outputs["job"] = (path, path.with_name("job.mp4"))
    used = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    daily.settings.max_media_storage_bytes = used + 100
    try:
        await daily.check_space()
        with pytest.raises(ApiError):
            await daily.check_space(1)
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_total_budget_keeps_unused_buffer_headroom(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.max_buffer_bytes = 200
    (service.buffer / "x.jpg").write_bytes(b"x" * 50)
    used = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    daily.settings.max_media_storage_bytes = used + 150
    try:
        await daily.check_space()
        with pytest.raises(ApiError):
            await daily.check_space(1)
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_standalone_jpeg_cannot_exceed_total_storage_budget(tmp_path):
    service = RecordingService(
        tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()), max_buffer_bytes=10000
    )
    service.max_storage_bytes = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    try:
        with pytest.raises(OSError, match="total storage budget"):
            await service.store_frame(FrameEnvelope("cam", 1, 1, b"x" * 4096))
        assert not list(service.buffer.glob("*.jpg"))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_buffer_budget_snapshot_invalidates_when_encoder_finishes(tmp_path):
    service = RecordingService(
        tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()), max_storage_bytes=2_000_000
    )
    try:
        await service.store_frame(FrameEnvelope("cam", 1, 1, b"x" * 4096))
        service.capacity_reservations["job"] = 1_000_000
        output = service.clips / "job.mp4"
        output.write_bytes(b"m" * 1_700_000)
        service.capacity_reservations.pop("job")
        service.max_storage_bytes = 1_700_001
        with pytest.raises(OSError, match="total storage budget"):
            await service.store_frame(FrameEnvelope("cam", 2, 2, b"x" * 4096))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_actual_encoded_output_cannot_become_ready_over_daily_budget(tmp_path, monkeypatch):
    import io

    import imageio_ffmpeg
    from PIL import Image

    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    raw = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(raw, "JPEG")
    daily.settings.max_daily_storage_bytes = len(raw.getvalue()) + 1
    try:
        await daily.tick(clock[0])
        await service.store_frame(FrameEnvelope("camera-main", clock[0], 1, raw.getvalue()))
        clock[0] += 10
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert not daily.catalog.segments(day["recording_id"], ready_only=True)
        assert not any(g["reason_code"] == "STORAGE_FULL" for g in day["gaps"])
        assert day["error"]["code"] == "INSUFFICIENT_STORAGE"
        assert daily.capacity_pending
        assert not list((tmp_path / "daily").rglob("*.ts"))
        assert not list((tmp_path / "daily").rglob("*.prepared.json"))
        assert not daily.storage_reservations
    finally:
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_recovery_rechecks_actual_output_budget_after_interrupted_publish(
    tmp_path, monkeypatch
):
    import asyncio
    import io

    import imageio_ffmpeg
    from PIL import Image

    from camera_media_service import daily_recording as module

    clock = [DailyWindow.from_date(date(2026, 11, 3)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    raw = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(raw, "JPEG")
    daily.settings.max_daily_storage_bytes = len(raw.getvalue()) + 1
    original = module.encode_daily_segment

    async def interrupt_after_publish(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError

    try:
        await daily.tick(clock[0])
        await service.store_frame(FrameEnvelope("camera-main", clock[0], 1, raw.getvalue()))
        monkeypatch.setattr(module, "encode_daily_segment", interrupt_after_publish)
        clock[0] += 10
        with pytest.raises(asyncio.CancelledError):
            await daily.tick(clock[0])
        assert list((tmp_path / "daily").rglob("*.ts"))
        await daily._recover_segments()
        assert (
            service.db.execute(
                "SELECT count(*) FROM daily_segments WHERE status='ready'"
            ).fetchone()[0]
            == 0
        )
        assert (
            service.db.execute(
                "SELECT count(*) FROM daily_segments WHERE status='failed'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await service.close()
