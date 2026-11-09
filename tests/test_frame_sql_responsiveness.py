import asyncio
import threading
import time

import pytest
from test_frame_packs import frames, service_at


class SlowStatements:
    def __init__(self, db, match):
        self.db, self.match = db, match
        self.main = threading.get_ident()
        self.entered, self.release = threading.Event(), threading.Event()

    def __getattr__(self, name):
        return getattr(self.db, name)

    def _check(self, sql):
        if self.match in sql:
            assert threading.get_ident() != self.main, "Frame SQL blocks the receiver loop"
            self.entered.set()
            assert self.release.wait(5)

    def execute(self, sql, *args):
        self._check(sql)
        return self.db.execute(sql, *args)

    def executemany(self, sql, *args):
        self._check(sql)
        return self.db.executemany(sql, *args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,match",
    [
        ("store", "INSERT INTO frames"),
        ("prune", "SELECT f.*,r.pack FROM frames"),
        ("prune", "SELECT pack,count(*)"),
        ("prune", "DELETE FROM frames WHERE path"),
        ("prune", "DELETE FROM frame_packs WHERE pack"),
    ],
)
async def test_frame_sql_drains_with_exclusive_connection_and_responsive_loop(
    tmp_path, operation, match
):
    service = service_at(tmp_path)
    source = frames(30)
    if operation == "prune":
        await service.store_frames(source)
    real = service.db
    slow = SlowStatements(real, match)
    service.db = service.frame_packs.db = slow

    async def run():
        if operation == "store":
            await service.store_frames(source)
        else:
            async with service.lock:
                await service._prune(time.time() + 400)

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(slow.entered.wait, 1), (
            str(task.exception()) if task.done() else "SQL not reached"
        )
        assert service.lock.locked()
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and service.lock.locked()
        slow.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service.lock.locked()
        if operation == "store":
            assert real.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
            assert real.execute("SELECT count(*) FROM frame_ranges").fetchone()[0] == 0
            assert not list(service.buffer.glob("*.jpack"))
        else:
            # Cancellation either retains the committed source or finishes its
            # unreferenced cleanup; it must never leave a live frame without data.
            for (pack,) in real.execute(
                "SELECT DISTINCT pack FROM frame_ranges r JOIN frames f ON f.path=r.path"
            ):
                assert (service.buffer / pack).is_file()
    finally:
        slow.release.set()
        await asyncio.gather(task, return_exceptions=True)
        service.db = service.frame_packs.db = real
        await service.close()


@pytest.mark.asyncio
async def test_prune_keeps_pinned_pack_when_sql_runs_on_worker(tmp_path):
    service = service_at(tmp_path)
    source = frames(40)
    try:
        await service.store_frames(source)
        async with service.lock:
            async with service.transaction():
                kept = service.pin_frames(
                    "reader", "camera-main", source[0].received_at, source[1].received_at
                )
            await service._prune(time.time() + 400)
        assert service.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 1
        assert service.frame_packs.read(kept[0]) == source[0].jpeg
    finally:
        await service.close()
