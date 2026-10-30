"""Deletion completes owned file cleanup exactly once across concurrent callers."""

import asyncio
import threading
from datetime import date

import pytest
import pytest_asyncio
from test_daily_export import make_exports
from test_daily_recording import make_daily

import camera_media_service.daily_export as module
from camera_media_service.daily_models import DailyWindow
from camera_media_service.recording import ApiError, iso


@pytest_asyncio.fixture
async def owned_day(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    with daily.db:
        day = daily.catalog.ensure_day("camera-main", window, 30)
        day.update(status="closed", expires_at=iso(window.end_at + 30 * 86400))
        daily.catalog.save_day(day)
    exports = make_exports(daily)
    daily.settings.daily_archive_enabled = False
    key = day["recording_id"]
    for category in ("daily", "daily-playback"):
        folder = service.root / category / key
        folder.mkdir(parents=True)
        (folder / "media.ts").write_bytes(b"retained media")
    exports._path(key).write_bytes(b"archive")
    try:
        yield exports, daily, key, clock
    finally:
        await exports.close()
        await daily.close()
        await service.close()


def queue_cleanup(exports, key):
    with exports.db:
        day = exports.daily.catalog.get_day(key)
        day.update(status="deleted", cleanup_state="pending")
        exports.daily.catalog.save_day(day)
        exports.db.execute("INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (key,))


def block_source_removal(exports, key, monkeypatch):
    source = exports.root / "daily" / key
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    real = module.shutil.rmtree

    def remove(path, *args, **kwargs):
        if path == source and not entered.is_set():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise RuntimeError("test did not release file worker")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", remove)
    return entered, release


async def assert_waiting(task):
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), 0.03)


@pytest.mark.asyncio
async def test_concurrent_deletes_wait_for_complete_cleanup(owned_day, monkeypatch):
    exports, daily, key, _ = owned_day
    entered, release = block_source_removal(exports, key, monkeypatch)
    first = asyncio.create_task(exports.delete_day(key))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(exports.delete_day(key))
    try:
        await assert_waiting(second)
        pending = await daily.get_day(key)
        assert pending["cleanup_state"] == "pending"
        assert pending["space_reclaimed"] is None
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert results == [None, None]
    complete = await daily.get_day(key)
    assert complete["cleanup_state"] == "complete"
    assert complete["cleanup_completed_at"] is not None
    assert complete["cleanup_error"] is None
    assert complete["space_reclaimed"] is None
    await exports.delete_day(key)
    assert await daily.get_day(key) == complete
    assert not exports._path(key).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("background", ["cleanup", "compact"])
async def test_delete_waits_for_same_day_background_file_work(owned_day, monkeypatch, background):
    exports, daily, key, _ = owned_day
    if background == "cleanup":
        queue_cleanup(exports, key)
        operation = exports._cleanup
    else:
        with exports.db:
            day = daily.catalog.get_day(key)
            day.update(archive_state="mp4", size_bytes=7)
            daily.catalog.save_day(day)
            exports.db.execute("INSERT INTO daily_compactions VALUES(?)", (key,))
        operation = exports._compact
    entered, release = block_source_removal(exports, key, monkeypatch)
    worker = asyncio.create_task(operation(key))
    await asyncio.wait_for(entered.wait(), 1)
    deletion = asyncio.create_task(exports.delete_day(key))
    try:
        await assert_waiting(deletion)
    finally:
        release.set()
        results = await asyncio.gather(worker, deletion, return_exceptions=True)
    assert all(not isinstance(result, BaseException) for result in results), results
    assert (await daily.get_day(key))["cleanup_state"] == "complete"
    assert not exports._path(key).exists()


@pytest.mark.asyncio
async def test_disappeared_owned_root_is_already_cleaned(owned_day, monkeypatch):
    exports, daily, key, _ = owned_day
    source = exports.root / "daily" / key
    real = module.shutil.rmtree

    def remove(path, *args, **kwargs):
        if path == source:
            real(path, *args, **kwargs)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", remove)
    await exports.delete_day(key)
    assert (await daily.get_day(key))["cleanup_state"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [PermissionError, OSError, FileNotFoundError])
async def test_partial_failure_remains_visible_and_retries(owned_day, monkeypatch, failure):
    exports, daily, key, clock = owned_day
    playback = exports.root / "daily-playback" / key
    real = module.shutil.rmtree

    def remove(path, *args, **kwargs):
        if path == playback:
            raise failure(13, "simulated storage failure", str(playback / "media.ts"))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", remove)
    with pytest.raises((OSError, ApiError)) as caught:
        await exports.delete_day(key)
    if isinstance(caught.value, ApiError):
        assert caught.value.status == 503
    failed = await daily.get_day(key)
    assert failed["status"] == "deleted"
    assert failed["cleanup_state"] == "failed"
    assert failed["cleanup_completed_at"] is None
    assert failed["cleanup_error"]["code"] == "STORAGE_ERROR"
    assert failed["space_reclaimed"] is None
    assert not (exports.root / "daily" / key).exists()
    assert exports.db.execute("SELECT 1 FROM daily_cleanup WHERE day_id=?", (key,)).fetchone()
    monkeypatch.setattr(module.shutil, "rmtree", real)
    await exports.maintenance(clock[0])
    assert (await daily.get_day(key))["cleanup_state"] == "complete"
    assert not playback.exists()
    assert not exports._path(key).exists()
    await exports.delete_day(key)


@pytest.mark.asyncio
async def test_startup_requeues_pending_cleanup_metadata(owned_day):
    exports, daily, key, _ = owned_day
    with exports.db:
        day = daily.catalog.get_day(key)
        day.update(status="deleted", cleanup_state="pending", cleanup_completed_at=None)
        daily.catalog.save_day(day)
    restarted = make_exports(daily)
    try:
        await restarted.start()
        assert (await daily.get_day(key))["cleanup_state"] == "complete"
        assert not restarted._path(key).exists()
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_pins_block_expiry_cleanup_and_delete(owned_day):
    exports, daily, key, clock = owned_day
    with exports.db:
        exports.db.execute("INSERT INTO clip_recording_pins VALUES(?,?)", ("clip-owner", key))
    with pytest.raises(ApiError) as error:
        await exports.delete_day(key)
    assert error.value.status == 409
    clock[0] += 31 * 86400
    await exports.maintenance(clock[0])
    assert (exports.root / "daily" / key / "media.ts").exists()
    pending = await daily.get_day(key)
    assert pending["status"] == "expired"
    assert pending["cleanup_state"] == "pending"
    with exports.db:
        exports.db.execute("DELETE FROM clip_recording_pins WHERE day_id=?", (key,))
    await exports.maintenance(clock[0])
    complete = await daily.get_day(key)
    assert complete["status"] == "expired"
    assert complete["cleanup_state"] == "complete"


@pytest.mark.asyncio
async def test_cancellation_keeps_file_serialization_until_thread_finishes(owned_day, monkeypatch):
    exports, daily, key, _ = owned_day
    entered, release = block_source_removal(exports, key, monkeypatch)
    first = asyncio.create_task(exports.delete_day(key))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    second = asyncio.create_task(exports.delete_day(key))
    try:
        await assert_waiting(second)
        assert not first.done()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    assert (await daily.get_day(key))["cleanup_state"] == "complete"
