import asyncio
import threading

import pytest
from test_daily_export import make_exports, recorded_day

from camera_media_service.recorded_clips import RecordedClipService
from camera_media_service.recording import iso


class CommitBarrier:
    def __init__(self, db, phase):
        self.db, self.phase = db, phase
        self.main = threading.get_ident()
        self.entered, self.release = threading.Event(), threading.Event()
        self.count = 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.commit() if kind is None else self.db.rollback()

    def commit(self):
        assert threading.get_ident() != self.main, "Range FULL commit blocks receiver loop"
        self.db.commit()
        self.count += 1
        if self.count == self.phase:
            self.entered.set()
            assert self.release.wait(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,phase", [("range", 1), ("range", 2), ("event", 1)])
async def test_extraction_commit_cancellation_leaves_no_orphan_job_or_pin(
    tmp_path, monkeypatch, operation, phase
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    clips = RecordedClipService(service, daily)
    await daily.metadata_writer.join()
    segment = daily.catalog.segments(old["recording_id"], ready_only=True)[0]
    real = service.db
    barrier = CommitBarrier(real, phase)
    service.db = daily.db = daily.catalog.db = clips.db = exports.db = barrier
    if operation == "range":
        call = clips.create_range(
            "camera-main",
            "range-commit-cancel",
            iso(segment["start_at"] + 0.1),
            iso(segment["end_at"] - 0.1),
        )
    else:
        call = clips.create_clip(
            "camera-main", "event-commit-cancel", iso(segment["start_at"] + 0.1)
        )
    task = asyncio.create_task(call)
    try:
        assert await asyncio.to_thread(barrier.entered.wait, 1), (
            str(task.exception()) if task.done() else "Commit not reached"
        )
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and service.lock.locked()
        barrier.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for key, status in real.execute("SELECT id,status FROM jobs"):
            assert status != "processing" or key in clips.jobs, (
                "Committed extraction lost its owning task"
            )
        await asyncio.gather(*clips.jobs.values())
        assert not real.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not service.lock.locked()
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
        service.db = daily.db = daily.catalog.db = clips.db = exports.db = real
        real.rollback()
        await clips.close()
        await exports.close()
        await daily.close()
        await service.close()
