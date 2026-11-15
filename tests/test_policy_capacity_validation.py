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
from camera_media_service.media_files import storage_io
from camera_media_service.policy_capacity import PolicyCapacity
from camera_media_service.recording import ApiError, iso
from camera_media_service.storage_status import StorageStatus

MIB = 1024**2


@pytest.mark.asyncio
async def test_capacity_probe_accounts_real_reservations_without_counting_rejection(
    tmp_path, monkeypatch
):
    clock = [DailyWindow.from_date(date(2026, 11, 15)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    daily.settings.max_daily_storage_bytes = 100 * MIB
    daily.settings.max_media_storage_bytes = 200 * MIB
    daily.settings.daily_archive_workspace_bytes = 10 * MIB
    service.max_buffer_bytes = 2 * MIB
    daily.settings.min_free_storage_bytes = 16 * MIB
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: SimpleNamespace(total=1024 * MIB, free=400 * MIB)
    )
    try:
        folder = tmp_path / "daily"
        folder.mkdir(exist_ok=True)
        (folder / "stored.ts").write_bytes(b"x" * (8 * MIB))
        output = folder / "pending.tmp.ts"
        output.write_bytes(b"x" * (2 * MIB))
        (service.buffer / "fixture.jpg").write_bytes(b"x" * MIB)
        daily.storage_reservations["job"] = 6 * MIB
        daily.storage_reservation_outputs["job"] = (output,)
        report = await daily.inspect_capacity()
        assert report["status"] == "available"
        assert report["available_recording_bytes"] == 76 * MIB
        assert report["budget"]["workspace_bytes"] == 10 * MIB
        assert report["budget"]["unallocated_reserved_bytes"] == 4 * MIB
        assert report["budget"]["buffer_headroom_bytes"] == MIB
        daily.settings.max_daily_storage_bytes = 19 * MIB
        before = dict(service.diagnostics.loss_counts)
        report = await daily.inspect_capacity()
        assert (
            report["status"] == "insufficient_capacity" and report["available_recording_bytes"] == 0
        )
        assert "daily_budget" in report["limiting_conditions"]
        assert service.diagnostics.loss_counts == before
        with pytest.raises(ApiError) as error:
            await daily.check_space(recording=True)
        assert error.value.code == "INSUFFICIENT_STORAGE"
        assert service.diagnostics.loss_counts["storage_rejected_daily_budget"] == 1
    finally:
        daily.storage_reservations.clear()
        daily.storage_reservation_outputs.clear()
        await daily.close()
        await service.close()


def owner(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 15)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    daily.settings.max_daily_storage_bytes = 45_000_000
    daily.settings.max_media_storage_bytes = 51_000_000
    daily.settings.daily_archive_workspace_bytes = 1_000_000
    daily.settings.min_free_storage_bytes = 1_000_000
    service.max_buffer_bytes = 2_000_000
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: SimpleNamespace(total=1_000_000_000, free=900_000_000)
    )
    window = DailyWindow.from_date(date(2026, 11, 14))
    with service.db:
        day = daily.catalog.ensure_day("camera-main", window, 30)
        day.update(
            status="closed",
            recorded_seconds=86400,
            archive_state="mp4",
            export_state="ready",
            size_bytes=8_000_000,
            expires_at=iso(clock[0] + 30 * 86400),
        )
        daily.catalog.save_day(day)
    exports = make_exports(daily)
    with (exports.output / (day["recording_id"] + ".mp4")).open("wb") as file:
        file.truncate(day["size_bytes"])

    async def idle(now):
        pass

    monkeypatch.setattr(exports, "maintenance", idle)
    return service, daily, exports, clock, day


@pytest.mark.asyncio
async def test_daily_policy_warns_for_proposed_retention_preview_never_writes(
    tmp_path, monkeypatch
):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    path = "/api/v2/cameras/camera-main/daily-recording-policy"
    app = create_http_app(
        service, daily=daily, exports=exports, thumbnail_options={"enabled": False}
    )
    async with TestClient(TestServer(app)) as client:
        original = daily.catalog.get_day(old["recording_id"])
        before = list(service.db.execute("SELECT * FROM daily_policies"))
        transitions = list(service.db.execute("SELECT * FROM daily_policy_transitions"))
        response = await client.post(
            path + "/validate", json={"enabled": True, "retention_days": 365}
        )
        assert response.status == 200
        result = await response.json()
        assert result["retention_days"] == 365
        validation = result["capacity_validation"]
        assert (
            validation["policy_applied"] is False
            and validation["status"] == "insufficient_capacity"
        )
        assert "RETENTION_CAPACITY_EXCEEDED" in {w["code"] for w in validation["warnings"]}
        assert validation["retention"]["cameras"][0]["retention_days"] == 365
        assert list(service.db.execute("SELECT * FROM daily_policies")) == before
        assert list(service.db.execute("SELECT * FROM daily_policy_transitions")) == transitions
        assert daily.catalog.get_day(old["recording_id"]) == original
        response = await client.put(path, json={"enabled": True, "retention_days": 1})
        assert response.status == 200
        result = await response.json()
        assert result["capacity_validation"]["status"] == "estimated_fit"
        assert result["capacity_validation"]["policy_applied"] is True
        assert (await (await client.get(path)).json()) == {"enabled": True, "retention_days": 1}
        assert daily.catalog.get_day(old["recording_id"]) == original
        response = await client.put(path, json={"enabled": True, "retention_days": 30})
        assert response.status == 200
        assert (await response.json())["capacity_validation"]["status"] == "insufficient_capacity"


@pytest.mark.asyncio
async def test_invalid_policy_no_write_and_clip_preview_reports_estimate_unknown(
    tmp_path, monkeypatch
):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    app = create_http_app(
        service, daily=daily, exports=exports, thumbnail_options={"enabled": False}
    )
    async with TestClient(TestServer(app)) as client:
        path = "/api/v2/cameras/camera-main/daily-recording-policy"
        for method in (client.post, client.put):
            target = path + "/validate" if method == client.post else path
            response = await method(target, json={"enabled": True, "retention_days": True})
            assert response.status == 400
        assert not service.db.execute("SELECT 1 FROM daily_policies").fetchone()
        clip = "/api/v1/cameras/camera-main/recording-policy"
        value = {"pre_roll_seconds": 20, "post_roll_seconds": 40, "retention_days": 10}
        response = await client.post(clip + "/validate", json=value)
        assert response.status == 200
        validation = (await response.json())["capacity_validation"]
        assert validation["policy_applied"] is False and validation["status"] == "unknown"
        assert "CLIP_RETENTION_ESTIMATE_UNAVAILABLE" in {w["code"] for w in validation["warnings"]}
        assert not service.db.execute("SELECT 1 FROM policies").fetchone()
        response = await client.put(clip, json=value)
        assert (
            response.status == 200
            and (await response.json())["capacity_validation"]["policy_applied"] is True
        )
        assert await (await client.get(clip)).json() == value


@pytest.mark.asyncio
async def test_full_storage_allows_explicit_disable_without_expiring_existing_media(
    tmp_path, monkeypatch
):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    daily.settings.max_daily_storage_bytes = 1
    async with TestClient(
        TestServer(
            create_http_app(
                service, daily=daily, exports=exports, thumbnail_options={"enabled": False}
            )
        )
    ) as client:
        original = daily.catalog.get_day(old["recording_id"])
        path = "/api/v2/cameras/camera-main/daily-recording-policy"
        response = await client.put(path, json={"enabled": False, "retention_days": 2})
        assert response.status == 200
        result = await response.json()
        assert result["capacity_validation"]["policy_applied"] is True
        assert "CURRENT_STORAGE_INSUFFICIENT" in {
            w["code"] for w in result["capacity_validation"]["warnings"]
        }
        assert result["capacity_validation"]["current"]["available_recording_bytes"] == 0
        assert daily.catalog.get_day(old["recording_id"]) == original
        assert (exports.output / (old["recording_id"] + ".mp4")).stat().st_size == old["size_bytes"]


@pytest.mark.asyncio
async def test_unknown_current_observation_never_reports_guaranteed_fit(tmp_path, monkeypatch):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)

    async def failed(**kwargs):
        raise OSError("private storage path must not escape")

    monkeypatch.setattr(daily, "inspect_capacity", failed)
    async with TestClient(
        TestServer(
            create_http_app(
                service, daily=daily, exports=exports, thumbnail_options={"enabled": False}
            )
        )
    ) as client:
        response = await client.post(
            "/api/v2/cameras/camera-main/daily-recording-policy/validate",
            json={"enabled": True, "retention_days": 1},
        )
        assert response.status == 200
        result = await response.json()
        assert result["capacity_validation"]["status"] == "unknown"
        assert "STORAGE_OBSERVATION_UNAVAILABLE" in {
            w["code"] for w in result["capacity_validation"]["warnings"]
        }
        assert "private storage" not in str(result)
        assert not service.db.execute("SELECT 1 FROM daily_policies").fetchone()


@pytest.mark.asyncio
async def test_forecast_includes_other_cameras_and_missing_samples_are_unknown(
    tmp_path, monkeypatch
):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    service.cameras.add("camera-second")
    storage = StorageStatus(service, daily)
    checker = PolicyCapacity(service, daily, storage)
    try:
        result = await checker.evaluate(
            "camera-main", {"enabled": True, "retention_days": 1}, kind="daily"
        )
        validation = result["capacity_validation"]
        assert validation["status"] == "unknown"
        assert len(validation["retention"]["cameras"]) == 2
        assert "CAPACITY_ESTIMATE_UNAVAILABLE" in {w["code"] for w in validation["warnings"]}
        assert not service.db.execute("SELECT 1 FROM daily_policies").fetchone()
    finally:
        await storage.close()
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_policy_checks_bound_admission_and_cancel_drains_worker(tmp_path, monkeypatch):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    storage = StorageStatus(service, daily)
    checker = PolicyCapacity(service, daily, storage)
    entered, release = threading.Event(), threading.Event()
    original = daily.inspect_capacity

    def block():
        entered.set()
        assert release.wait(5)

    async def slow(**kwargs):
        await storage_io(block)
        return await original(**kwargs)

    monkeypatch.setattr(daily, "inspect_capacity", slow)
    value = {"enabled": True, "retention_days": 1}
    tasks = [asyncio.create_task(checker.evaluate("camera-main", value, kind="daily"))]
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        tasks.extend(
            asyncio.create_task(checker.evaluate("camera-main", value, kind="daily"))
            for _ in range(3)
        )
        await asyncio.sleep(0.01)
        with pytest.raises(ApiError) as error:
            await checker.evaluate("camera-main", value, kind="daily")
        assert error.value.code == "SERVICE_BUSY"
        tasks[0].cancel()
        tasks[0].cancel()
        await asyncio.sleep(0.01)
        assert not tasks[0].done() and checker.slot.locked() and checker.admitted == 4
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert all(not isinstance(x, BaseException) for x in results[1:])
        assert checker.admitted == 0 and not checker.slot.locked()
        assert not service.db.execute("SELECT 1 FROM daily_policies").fetchone()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await storage.close()
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_clip_only_cancel_retains_slot_until_legacy_worker_finishes(tmp_path, monkeypatch):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    storage = StorageStatus(service)
    checker = PolicyCapacity(service, None, storage)
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(5)

    async def legacy_check():
        await asyncio.to_thread(block)

    monkeypatch.setattr(service, "check_media_space", legacy_check)
    task = asyncio.create_task(
        checker.evaluate(
            "camera-main",
            {"pre_roll_seconds": 10, "post_roll_seconds": 10, "retention_days": 30},
            kind="clip",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and checker.slot.locked() and checker.admitted == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert checker.admitted == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_clip_probe_uses_clip_admission_without_daily_only_workspace(tmp_path, monkeypatch):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    daily.settings.max_daily_storage_bytes = 8_500_000
    storage = StorageStatus(service, daily)
    checker = PolicyCapacity(service, daily, storage)
    try:
        await daily.check_space(recording=False)
        with pytest.raises(ApiError):
            await daily.check_space(recording=True)
        result = await checker.evaluate(
            "camera-main",
            {"pre_roll_seconds": 10, "post_roll_seconds": 10, "retention_days": 30},
            kind="clip",
        )
        assert result["capacity_validation"]["current"]["status"] == "available"
        assert result["capacity_validation"]["current"]["budget"]["workspace_bytes"] == 0
        assert "CURRENT_STORAGE_INSUFFICIENT" not in {
            w["code"] for w in result["capacity_validation"]["warnings"]
        }
    finally:
        await storage.close()
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_cancel_during_storage_observation_drains_before_releasing_slot(
    tmp_path, monkeypatch
):
    service, daily, exports, clock, old = owner(tmp_path, monkeypatch)
    storage = StorageStatus(service, daily)
    checker = PolicyCapacity(service, daily, storage)
    entered, release = threading.Event(), threading.Event()
    original = storage._observe

    def slow(state):
        entered.set()
        assert release.wait(5)
        return original(state)

    monkeypatch.setattr(storage, "_observe", slow)
    task = asyncio.create_task(
        checker.evaluate("camera-main", {"enabled": True, "retention_days": 1}, kind="daily")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and checker.slot.locked() and checker.admitted == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert checker.admitted == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await storage.close()
        await exports.close()
        await daily.close()
        await service.close()
