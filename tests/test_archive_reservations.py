"""Archive reservations protect future writes without charging allocated bytes twice."""

import asyncio
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_daily_export import make_exports
from test_daily_recording import make_daily

import camera_media_service.daily_export as export_module
import camera_media_service.daily_recording as recording_module
from camera_media_service.daily_models import DailyWindow
from camera_media_service.recording import ApiError, iso


@pytest_asyncio.fixture
async def storage(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    for category in ("daily", "daily-exports", "daily-playback"):
        (service.root / category).mkdir(exist_ok=True)
    source = service.root / "daily" / "source.ts"
    source.write_bytes(b"s" * 600)
    temporary = service.root / "daily-exports" / "archive.tmp.mp4"
    final = temporary.with_name("archive.mp4")
    try:
        yield daily, source, temporary, final
    finally:
        await daily.close()
        await service.close()


def reserve_archive(daily, temporary, final, monkeypatch, *, output_bytes=300):
    temporary.write_bytes(b"a" * output_bytes)
    daily.storage_reservations["archive"] = 400
    daily.storage_reservation_outputs["archive"] = (temporary, final)
    daily.settings.max_daily_storage_bytes = 1050
    daily.settings.min_free_storage_bytes = 50
    monkeypatch.setattr(recording_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=200))


@pytest.mark.asyncio
@pytest.mark.parametrize("renamed", [False, True])
async def test_partial_archive_only_reserves_unallocated_bytes_for_both_limits(
    storage, monkeypatch, renamed
):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    if renamed:
        temporary.replace(final)
    # 600 source + 300 output + (400 planned - 300 allocated) + 50 new = 1050.
    # 200 actual free - 100 future archive - 50 new = 50 minimum.
    await daily.check_space(50)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["quota", "free"])
async def test_remaining_future_bytes_still_enforce_exact_limits(storage, monkeypatch, limit):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    if limit == "quota":
        daily.settings.max_daily_storage_bytes = 1049
    else:
        monkeypatch.setattr(
            recording_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=199)
        )
    with pytest.raises(ApiError) as error:
        await daily.check_space(50)
    assert error.value.code == "INSUFFICIENT_STORAGE"


@pytest.mark.asyncio
async def test_generic_reservations_remain_fully_reserved(storage, monkeypatch):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    daily.storage_reservations["generic"] = 100
    daily.settings.max_daily_storage_bytes = 1099
    monkeypatch.setattr(recording_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000))
    with pytest.raises(ApiError):
        await daily.check_space()


@pytest.mark.asyncio
async def test_output_larger_than_plan_does_not_reduce_other_reservations(storage, monkeypatch):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch, output_bytes=500)
    daily.storage_reservations["generic"] = 100
    daily.settings.max_daily_storage_bytes = 1199
    monkeypatch.setattr(recording_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=1000))
    with pytest.raises(ApiError):
        await daily.check_space()
    daily.settings.max_daily_storage_bytes = 1200
    await daily.check_space()


@pytest.mark.asyncio
@pytest.mark.parametrize("quota,admitted", [(999, False), (1000, True)])
async def test_growing_output_uses_one_stat_for_usage_and_reservation_credit(
    storage, monkeypatch, quota, admitted
):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    daily.settings.max_daily_storage_bytes = quota
    real_stat = Path.stat
    observations = []

    def growing_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == temporary:
            observations.append(info.st_size)
            # It grows after the scan observed 300 bytes. A separate credit stat
            # must not deduct these uncounted bytes from the future reservation.
            with path.open("ab") as output:
                output.write(b"g" * 100)
        return info

    monkeypatch.setattr(Path, "stat", growing_stat)
    if admitted:
        await daily.check_space()
    else:
        with pytest.raises(ApiError):
            await daily.check_space()
    assert observations == [300]


@pytest.mark.asyncio
async def test_scan_uses_reservation_snapshot_when_live_job_maps_change(storage, monkeypatch):
    daily, source, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    daily.settings.max_daily_storage_bytes = 999
    real_stat = Path.stat
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_stat(path, *args, **kwargs):
        if path == source:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise RuntimeError("test did not release storage scan")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocked_stat)
    checking = asyncio.create_task(daily.check_space())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        daily.storage_reservations.clear()
        daily.storage_reservation_outputs.clear()
    finally:
        release.set()
    with pytest.raises(ApiError):
        await checking


@pytest.mark.asyncio
async def test_rename_before_stat_keeps_full_unobserved_output_reservation(storage, monkeypatch):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    real_glob, real_stat = Path.rglob, Path.stat

    def listed_before_rename(path, pattern, *args, **kwargs):
        if path == temporary.parent:
            yield temporary
        else:
            yield from real_glob(path, pattern, *args, **kwargs)

    def renamed_stat(path, *args, **kwargs):
        if path == temporary:
            temporary.replace(final)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", listed_before_rename)
    monkeypatch.setattr(Path, "stat", renamed_stat)
    # The scan did not count the renamed 300 bytes: retain all 400 reserved.
    daily.settings.max_daily_storage_bytes = 999
    with pytest.raises(ApiError) as error:
        await daily.check_space()
    assert error.value.code == "INSUFFICIENT_STORAGE"


@pytest.mark.asyncio
async def test_file_disappearing_during_scan_does_not_break_recording_admission(
    storage, monkeypatch
):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    real_stat = Path.stat

    def disappearing_stat(path, *args, **kwargs):
        if path == temporary:
            path.unlink(missing_ok=True)
            raise FileNotFoundError(2, "output moved or removed", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappearing_stat)
    daily.settings.min_free_storage_bytes = 0
    monkeypatch.setattr(recording_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=400))
    await daily.check_space()


@pytest.mark.asyncio
async def test_storage_permission_failure_is_not_hidden_by_scan(storage, monkeypatch):
    daily, _, temporary, final = storage
    reserve_archive(daily, temporary, final, monkeypatch)
    real_stat = Path.stat

    def inaccessible_stat(path, *args, **kwargs):
        if path == temporary:
            raise PermissionError(13, "cannot inspect output", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible_stat)
    with pytest.raises(PermissionError):
        await daily.check_space()


def closed_day(daily, source):
    window = DailyWindow.from_date(date(2026, 10, 14))
    with daily.db:
        day = daily.catalog.ensure_day("camera-main", window, 30)
        day.update(status="closed", recorded_seconds=1, expires_at=iso(window.end_at + 86400))
        daily.catalog.save_day(day)
        segment = daily.catalog.begin_segment(day["recording_id"], window.end_at - 1, window.end_at)
        daily.catalog.complete_segment(
            segment["segment_id"],
            {
                "duration": 1,
                "size_bytes": 600,
                "path": str(source.relative_to(daily.recording.root)),
            },
        )
    return day["recording_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
async def test_verification_keeps_job_active_without_future_output_reservation(
    storage, monkeypatch, reuse
):
    daily, source, _, _ = storage
    key = closed_day(daily, source)
    exports = make_exports(daily)
    verification_started, finish = asyncio.Event(), asyncio.Event()
    if reuse:
        exports._path(key).write_bytes(b"a" * 300)

    async def merge(_, segments, temporary, final):
        assert not reuse
        assert segments[0]["size_bytes"] == 600
        temporary.write_bytes(b"a" * 300)
        temporary.replace(final)

    async def verify(_, final, segments):
        verification_started.set()
        await finish.wait()
        return {"size_bytes": final.stat().st_size}

    monkeypatch.setattr(export_module, "merge_daily_mp4", merge)
    monkeypatch.setattr(export_module, "verify_archive", verify)
    try:
        await exports._enqueue(key)
        expected = (exports.output / (key + ".tmp.mp4"), exports._path(key))
        assert daily.storage_reservation_outputs.get(key) == expected
        await asyncio.wait_for(verification_started.wait(), 1)
        assert not exports.jobs[key].done()
        assert daily.storage_reservations[key] == 0
        daily.settings.max_daily_storage_bytes = 950
        daily.settings.min_free_storage_bytes = 50
        monkeypatch.setattr(
            recording_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=100)
        )
        await daily.check_space(50)
        finish.set()
        await exports.jobs[key]
        await asyncio.sleep(0)
        assert key not in daily.storage_reservations
        assert key not in daily.storage_reservation_outputs
    finally:
        finish.set()
        await exports.close()


@pytest.mark.asyncio
async def test_archive_cancelled_before_start_releases_reservation_and_output_mapping(storage):
    daily, source, _, _ = storage
    key = closed_day(daily, source)
    exports = make_exports(daily)
    try:
        await exports._enqueue(key)
        assert key in daily.storage_reservations
        assert key in daily.storage_reservation_outputs
        await exports.close()
        assert not daily.storage_reservations
        assert not daily.storage_reservation_outputs
        assert (await daily.get_day(key))["export_state"] == "failed"
    finally:
        await exports.close()
