import asyncio
import sqlite3
import threading
import time

import pytest
from test_daily_export import recorded_day
from test_recording import make_service


@pytest.mark.asyncio
async def test_metadata_worker_checks_mount_guard_before_opening_database(tmp_path):
    from camera_media_service.persisted_media_info import persist_generated_info

    service = make_service(tmp_path)

    def denied():
        raise OSError("Expected SD mount unavailable")

    service.storage_guard = denied
    try:
        result = await persist_generated_info(
            service,
            tmp_path / "owned.mp4",
            {"_file_identity": (1, 2, 3, 4, 5), "media_info": {"status": "ready"}},
        )
        assert result is False
        assert not (tmp_path / "media-info.sqlite3").exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_daily_metadata_commit_leaves_loop_and_primary_catalogue_available(
    tmp_path, monkeypatch
):
    import camera_media_service.persisted_media_info as module

    service, daily, clock, _ = await recorded_day(tmp_path, monkeypatch)
    await daily.metadata_writer.join()
    original = module._optional_write
    observed, commits = [], []

    def slow(*args, **kwargs):
        started = time.monotonic()
        time.sleep(0.2)  # Reproduce a measured SD metadata commit stall.
        try:
            return original(*args, **kwargs)
        finally:
            commits.append((started, time.monotonic()))

    async def heartbeat():
        while True:
            async with service.lock:
                observed.append(time.monotonic())
            await asyncio.sleep(0.005)

    monkeypatch.setattr(module, "_optional_write", slow)
    from test_daily_recording import jpeg

    from camera_media_service.pipeline import FrameEnvelope

    for i in range(10):
        await service.store_frame(FrameEnvelope("camera-main", clock[0] + i / 10, i, jpeg()))
    clock[0] += 10
    heartbeat_task = asyncio.create_task(heartbeat())
    tick = asyncio.create_task(daily.tick(clock[0]))
    try:
        await asyncio.wait_for(tick, 5)
        await asyncio.wait_for(daily.metadata_writer.join(), 5)
        assert commits
        assert all(any(a + 0.02 < sample < b - 0.02 for sample in observed) for a, b in commits), (
            "Metadata commit blocked the event loop or the primary recording lock"
        )
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(tick, heartbeat_task, return_exceptions=True)
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_archive_result_serialization_does_not_block_receipt_loop(tmp_path, monkeypatch):
    from test_daily_archive import archive_ready
    from test_daily_export import make_exports

    service, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    original = daily.catalog.save_day
    commits, samples = [], []

    def slow(body, **kwargs):
        if body["export_state"] == "ready":
            a = time.monotonic()
            time.sleep(0.15)
            commits.append((a, time.monotonic()))
        return original(body, **kwargs)

    async def heartbeat():
        while True:
            samples.append(time.monotonic())
            await asyncio.sleep(0.005)

    monkeypatch.setattr(daily.catalog, "save_day", slow)
    task = asyncio.create_task(heartbeat())
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, day["recording_id"]))["export_state"] == "ready"
        assert commits and any(commits[0][0] + 0.02 < t < commits[0][1] - 0.02 for t in samples)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_policy_commit_does_not_block_receipt_loop(tmp_path, monkeypatch):
    commits, samples = [], []

    class SlowCommit(sqlite3.Connection):
        def commit(self):
            a = time.monotonic()
            time.sleep(0.15)
            result = super().commit()
            commits.append((a, time.monotonic()))
            return result

        def __exit__(self, kind, value, traceback):
            if kind is None:
                self.commit()
            else:
                self.rollback()

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=SlowCommit))
    service = make_service(tmp_path)
    commits.clear()

    async def heartbeat():
        while True:
            samples.append(time.monotonic())
            await asyncio.sleep(0.005)

    task = asyncio.create_task(heartbeat())
    try:
        await service.set_policy(
            "camera-main", {"pre_roll_seconds": 15, "post_roll_seconds": 15, "retention_days": 30}
        )
        assert commits
        assert any(commits[0][0] + 0.02 < sample < commits[0][1] - 0.02 for sample in samples)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_metadata_write_closes_worker_connection_after_commit(
    tmp_path, monkeypatch
):
    import camera_media_service.persisted_media_info as module
    from camera_media_service.persisted_media_info import file_identity, persist_generated_info

    service = make_service(tmp_path)
    path = tmp_path / "owned.mp4"
    path.write_bytes(b"identity-bound fixture")
    entered, release = threading.Event(), threading.Event()
    operation = module._optional_write
    connections = []
    connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=ObservedConnection)
    )

    def slow(*args, **kwargs):
        connections.append(kwargs["connection"])
        entered.set()
        assert release.wait(2)
        return operation(*args, **kwargs)

    monkeypatch.setattr(module, "_optional_write", slow)
    task = asyncio.create_task(
        persist_generated_info(
            service,
            path,
            {
                "_file_identity": file_identity(path.stat()),
                "media_info": {"status": "ready", "codec": "h264"},
            },
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(connections) == 1
        assert connections[0].closed
        with sqlite3.connect(tmp_path / "media-info.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM measured_media_files").fetchone()[0] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
