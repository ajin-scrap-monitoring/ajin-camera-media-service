import asyncio
import shutil
import threading
from datetime import date
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_export import make_exports
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.http_api import create_http_app


def owner(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 9)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    daily.settings.max_media_storage_bytes = 60_000_000
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: SimpleNamespace(total=1_000_000_000, free=800_000_000)
    )
    return service, daily, clock


@pytest.mark.asyncio
async def test_storage_api_reports_scoped_bytes_and_no_side_effects(tmp_path, monkeypatch):
    service, daily, clock = owner(tmp_path, monkeypatch)
    for folder, size in [
        ("daily", 111),
        ("daily-exports", 222),
        ("daily-playback", 333),
        ("daily-thumbnails", 444),
    ]:
        directory = tmp_path / folder
        directory.mkdir(exist_ok=True)
        (directory / "fixture.bin").write_bytes(b"x" * size)
    (service.buffer / "raw-fixture.bin").write_bytes(b"x" * 9999)
    output = tmp_path / "daily" / "current.tmp.ts"
    output.write_bytes(b"x" * 100)
    daily.storage_reservations["day"] = 900
    daily.storage_reservation_outputs["day"] = (output,)
    clip = service.clips / "test.tmp.mp4"
    clip.write_bytes(b"x" * 300)
    daily.storage_reservations["clip"] = 800
    daily.storage_reservation_outputs["clip"] = (clip,)
    exports = make_exports(daily)
    async with TestClient(
        TestServer(create_http_app(service, daily=daily, exports=exports))
    ) as client:
        response = await client.get("/api/v1/storage/status")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = await response.json()
        assert body["status"] == "ready" and body["observed_at"].endswith("Z")
        assert body["storage_type"] == "filesystem"
        assert body["filesystem_total_bytes"] == 1_000_000_000
        assert body["filesystem_available_bytes"] == 800_000_000
        assert body["media_limit_bytes"] == 60_000_000
        assert body["daily_limit_bytes"] == 100_000_000
        assert body["daily_usage_bytes"] == 1210
        assert body["daily_reserved_bytes"] == 800
        assert body["reason_code"] is None
        assert str(tmp_path) not in str(body)
        assert output.read_bytes() == b"x" * 100 and clip.read_bytes() == b"x" * 300
        assert not exports.jobs
        assert (await client.get("/api/v1/storage/status?path=/")).status == 400
        daily.storage_reservations.clear()
        daily.storage_reservation_outputs.clear()


@pytest.mark.asyncio
async def test_cache_preserves_observation_time_and_invalidates_generation(tmp_path, monkeypatch):
    from camera_media_service.storage_status import StorageStatus

    service, daily, clock = owner(tmp_path, monkeypatch)
    reader = StorageStatus(service, daily)
    original = reader._observe
    calls = []

    def tracked(snapshot):
        calls.append(1)
        return original(snapshot)

    monkeypatch.setattr(reader, "_observe", tracked)
    try:
        first = await reader.snapshot()
        clock[0] += 1
        second = await reader.snapshot()
        assert first == second and len(calls) == 1
        daily.storage_reservations["new"] = 100
        daily.storage_reservation_outputs["new"] = (tmp_path / "daily" / "new.tmp.ts",)
        third = await reader.snapshot()
        assert len(calls) == 2
        assert third["observed_at"] != first["observed_at"]
        assert third["daily_reserved_bytes"] == 100
    finally:
        await reader.close()
        daily.storage_reservations.clear()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_cached_status_rechecks_guard_and_returns_unknown_not_zero(tmp_path, monkeypatch):
    from camera_media_service.storage_status import StorageStatus

    service, daily, clock = owner(tmp_path, monkeypatch)
    reader = StorageStatus(service, daily)
    try:
        assert (await reader.snapshot())["status"] == "ready"

        def failed():
            raise OSError("Private device path must not be disclosed")

        service.storage_guard = failed
        body = await reader.snapshot()
        assert body["status"] == "unavailable" and body["reason_code"] == "STORAGE_UNAVAILABLE"
        assert body["observed_at"] is None and body["filesystem_available_bytes"] is None
        assert body["daily_usage_bytes"] is None
        assert "Private device" not in str(body)
    finally:
        service.storage_guard = lambda: None
        await reader.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_status_reads_coalesce_and_one_cancel_does_not_detach_worker(
    tmp_path, monkeypatch
):
    from camera_media_service.storage_status import StorageStatus

    service, daily, clock = owner(tmp_path, monkeypatch)
    reader = StorageStatus(service, daily)
    entered, release = threading.Event(), threading.Event()
    original = reader._observe
    calls = []

    def blocked(snapshot):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(snapshot)

    monkeypatch.setattr(reader, "_observe", blocked)
    first = asyncio.create_task(reader.snapshot())
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(reader.snapshot())
        await asyncio.sleep(0.02)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        release.set()
        assert (await second)["status"] == "ready"
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await reader.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_status_returns_busy_after_bounded_reservation_churn(tmp_path, monkeypatch):
    from camera_media_service.storage_status import StorageStatus

    service, daily, clock = owner(tmp_path, monkeypatch)
    reader = StorageStatus(service, daily)
    loop = asyncio.get_running_loop()
    original = reader._observe
    calls = []

    def unstable(snapshot):
        result = original(snapshot)
        calls.append(1)
        changed = threading.Event()

        def change():
            daily.storage_reservations["changing"] = len(calls) * 10
            changed.set()

        loop.call_soon_threadsafe(change)
        assert changed.wait(3)
        return result

    monkeypatch.setattr(reader, "_observe", unstable)
    try:
        body = await reader.snapshot()
        assert body["status"] == "unavailable" and body["reason_code"] == "STORAGE_SNAPSHOT_BUSY"
        assert body["observed_at"] is None and body["daily_reserved_bytes"] is None
        assert len(calls) == 2
    finally:
        daily.storage_reservations.clear()
        await reader.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_close_during_mount_check_cannot_start_a_late_snapshot(tmp_path, monkeypatch):
    from camera_media_service.storage_status import StorageStatus

    service, daily, clock = owner(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def guard():
        entered.set()
        assert release.wait(5)

    service.storage_guard = guard
    reader = StorageStatus(service, daily)
    request = asyncio.create_task(reader.snapshot())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await reader.close()
        release.set()
        assert (await request)["status"] == "unavailable"
        assert reader._flight is None
    finally:
        release.set()
        service.storage_guard = lambda: None
        await asyncio.gather(request, return_exceptions=True)
        await reader.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_storage_api_returns_503_nullable_observation_on_mount_failure(tmp_path, monkeypatch):
    service, daily, clock = owner(tmp_path, monkeypatch)
    exports = make_exports(daily)
    async with TestClient(
        TestServer(create_http_app(service, daily=daily, exports=exports))
    ) as client:

        def failed():
            raise OSError("Unavailable managed mount")

        service.storage_guard = failed
        try:
            response = await client.get("/api/v1/storage/status")
            assert response.status == 503 and response.headers["Cache-Control"] == "no-store"
            body = await response.json()
            assert body["status"] == "unavailable" and body["observed_at"] is None
            assert body["filesystem_total_bytes"] is None
            assert body["filesystem_available_bytes"] is None
            assert body["daily_reserved_bytes"] is None
        finally:
            service.storage_guard = lambda: None
