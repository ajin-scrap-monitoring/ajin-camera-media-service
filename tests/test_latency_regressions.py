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


class SlowCatalog:
    def __init__(self, connection):
        self.connection = connection
        self.slow_threads = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if sql.endswith("status='ready' ORDER BY start,id"):
            self.slow_threads.append(threading.get_ident())
            time.sleep(0.04)
        return self.connection.execute(sql, *args)

    def commit(self):
        if self.connection.in_transaction:
            self.slow_threads.append(threading.get_ident())
            time.sleep(0.04)
        return self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.commit()
        else:
            self.connection.rollback()


@pytest.mark.asyncio
async def test_daily_snapshot_and_full_commit_do_not_block_live_event_loop(tmp_path, monkeypatch):
    from test_daily_export import recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    original = recording.db
    proxy = SlowCatalog(original)
    recording.db = daily.db = daily.catalog.db = proxy
    owner = threading.get_ident()
    try:
        clock[0] += 10
        await daily.tick(clock[0])
        assert proxy.slow_threads and owner not in proxy.slow_threads
        assert original.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert original.execute("SELECT cursor FROM daily_cursors").fetchone()[0] == clock[0]
    finally:
        recording.db = daily.db = daily.catalog.db = original
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_cancelled_full_commit_retains_lock_and_drains_before_reuse(tmp_path):
    import asyncio
    import sqlite3

    from camera_media_service.pipeline import LiveFrameHub
    from camera_media_service.recording import RecordingService
    from camera_media_service.store import LatestFrameStore

    recording = RecordingService(tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()))
    original = recording.db
    original.execute("CREATE TABLE transaction_probe(value INTEGER)")
    original.commit()
    entered, release = threading.Event(), threading.Event()

    class GateCatalog(SlowCatalog):
        def commit(self):
            entered.set()
            assert release.wait(5)
            return self.connection.commit()

    recording.db = GateCatalog(original)

    async def writer():
        async with recording.lock:
            async with recording.transaction():
                recording.db.execute("INSERT INTO transaction_probe VALUES(1)")

    task = asyncio.create_task(writer())
    waiter = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with sqlite3.connect(tmp_path / "catalog.sqlite3") as observer:
            assert observer.execute("SELECT count(*) FROM transaction_probe").fetchone()[0] == 0
        waiter = asyncio.create_task(recording.get_policy("cam"))
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert recording.lock.locked() and not task.done() and not waiter.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await waiter
        with sqlite3.connect(tmp_path / "catalog.sqlite3") as observer:
            assert observer.execute("SELECT count(*) FROM transaction_probe").fetchone()[0] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        recording.db = original
        await recording.close()


@pytest.mark.asyncio
async def test_failed_full_commit_rolls_back_before_catalogue_unlock(tmp_path):
    import sqlite3

    from camera_media_service.pipeline import LiveFrameHub
    from camera_media_service.recording import RecordingService
    from camera_media_service.store import LatestFrameStore

    recording = RecordingService(tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()))
    original = recording.db
    original.execute("CREATE TABLE transaction_probe(value INTEGER)")
    original.commit()

    class FailedCatalog(SlowCatalog):
        def commit(self):
            raise sqlite3.OperationalError("SD sync failed")

    recording.db = FailedCatalog(original)
    try:
        with pytest.raises(sqlite3.OperationalError, match="SD sync failed"):
            async with recording.lock:
                async with recording.transaction():
                    recording.db.execute("INSERT INTO transaction_probe VALUES(1)")
        assert not original.in_transaction
        assert original.execute("SELECT count(*) FROM transaction_probe").fetchone()[0] == 0
        assert not recording.lock.locked()
    finally:
        recording.db = original
        await recording.close()


@pytest.mark.asyncio
async def test_legacy_metadata_read_does_not_share_writer_connection(tmp_path):
    import json

    from camera_media_service.persisted_media_info import (
        close_probe_database,
        ensure_probe_table,
        file_identity,
        load_measured_info,
    )
    from camera_media_service.pipeline import LiveFrameHub
    from camera_media_service.recording import RecordingService
    from camera_media_service.store import LatestFrameStore

    recording = RecordingService(tmp_path, {"cam"}, LiveFrameHub(LatestFrameStore()))
    path = recording.clips / "probe.mp4"
    path.write_bytes(b"file")
    identity = file_identity(path.stat())
    info = {"status": "ready", "size_bytes": 4}
    with recording.db:
        ensure_probe_table(recording.db)
        recording.db.execute(
            "INSERT INTO measured_media_files VALUES(?,?,?)",
            ("clips/probe.mp4", json.dumps(identity), json.dumps(info)),
        )

    class Writer(SlowCatalog):
        def execute(self, sql, *args):
            if sql.startswith("SELECT identity,media_info"):
                raise AssertionError("Legacy read waited on writer connection mutex")
            return super().execute(sql, *args)

    owner = Writer(recording.db)
    try:
        assert load_measured_info(owner, tmp_path, path, identity) == info
    finally:
        close_probe_database(owner)
        await recording.close()
