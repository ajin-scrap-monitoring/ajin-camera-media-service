import asyncio
import sqlite3
import threading
from datetime import date

import pytest
from test_daily_archive import archive_ready
from test_daily_export import make_exports, recorded_day
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.media_details import MediaDetails


@pytest.mark.asyncio
async def test_workspace_history_read_uses_owned_worker_connection(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 9)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    daily.settings.daily_archive_workspace_bytes = 100
    main = threading.get_ident()
    original_state = daily.catalog.get_state
    original_connect = sqlite3.connect
    opened = []

    def state(key):
        assert threading.get_ident() != main, "Workspace catalogue read blocks receiver loop"
        return original_state(key)

    def connect(path, *args, **kwargs):
        if "?mode=ro" in str(path):
            assert threading.get_ident() != main
            opened.append(str(path))
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(daily.catalog, "get_state", state)
    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        await daily.check_space(recording=True)
        assert opened, "Workspace lookup must not share the live writer connection"
    finally:
        monkeypatch.setattr(daily.catalog, "get_state", original_state)
        monkeypatch.setattr(sqlite3, "connect", original_connect)
        await daily.close()
        await service.close()


class DelayedCommit:
    def __init__(self, db, entered, release):
        self.db, self.entered, self.release = db, entered, release
        self.main_thread = threading.get_ident()
        self.first = True

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.commit()
        else:
            self.db.rollback()

    def commit(self):
        assert threading.get_ident() != self.main_thread, "FULL pin commit blocks receiver loop"
        self.db.commit()
        if self.first:
            self.first = False
            self.entered.set()
            assert self.release.wait(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["media-info", "archive-hls"])
async def test_pin_commit_keeps_loop_responsive_and_cancelled_acquisition_releases_pin(
    tmp_path, monkeypatch, operation
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    key = old["recording_id"]
    if operation == "archive-hls":
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["export_state"] == "ready"
        while exports.jobs:
            await asyncio.sleep(0)
    await daily.metadata_writer.join()
    segment = daily.catalog.segments(key, ready_only=True)[0]["segment_id"]
    original = service.db
    entered, release = threading.Event(), threading.Event()
    proxy = DelayedCommit(original, entered, release)
    service.db = daily.db = daily.catalog.db = exports.db = proxy
    details = MediaDetails(service, daily, exports)
    call = (
        details.day(key)
        if operation == "media-info"
        else exports.playback.open_segment(key, segment)
    )
    task = asyncio.create_task(call)
    try:
        assert await asyncio.to_thread(entered.wait, 1), (
            str(task.exception()) if task.done() else "No pin commit"
        )
        assert service.lock.locked()
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and service.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not original.execute(
            "SELECT 1 FROM clip_recording_pins WHERE clip_id LIKE 'info:%' OR clip_id LIKE 'hls:%'"
        ).fetchone()
        assert not service.lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        original.rollback()
        service.db = daily.db = daily.catalog.db = exports.db = original
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_media_info_catalogue_snapshot_read_is_off_receiver_loop(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    original = daily.catalog.get_day
    main = threading.get_ident()

    def checked(key):
        assert threading.get_ident() != main, "Cold catalogue read blocks receiver loop"
        return original(key)

    monkeypatch.setattr(daily.catalog, "get_day", checked)
    try:
        body = await MediaDetails(service, daily, exports).day(old["recording_id"])
        assert body["segments"]["measured"] > 0
    finally:
        monkeypatch.setattr(daily.catalog, "get_day", original)
        await exports.close()
        await daily.close()
        await service.close()
