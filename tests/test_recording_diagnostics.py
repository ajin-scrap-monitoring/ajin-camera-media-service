import asyncio

import pytest

from camera_media_service.recording_diagnostics import RecordingDiagnostics, RecordingLock


def test_diagnostics_are_bounded_and_distinguish_loss_causes():
    stats = RecordingDiagnostics()
    for _ in range(2):
        stats.count("ingest_queue_full")
    stats.count("source_frames_missing")
    with stats.measure("coverage_refresh"):
        pass
    for i in range(1000):
        stats.count("cause_" + str(i))
    result = stats.snapshot()
    assert result["loss_counts"]["ingest_queue_full"] == 2
    assert result["loss_counts"]["source_frames_missing"] == 1
    assert len(result["loss_counts"]) <= 65
    assert result["stages"]["coverage_refresh"]["count"] == 1


@pytest.mark.asyncio
async def test_lock_instruments_wait_and_hold_without_changing_exclusion():
    stats = RecordingDiagnostics()
    lock = RecordingLock(stats)
    entered = asyncio.Event()

    async def waiter():
        async with lock:
            entered.set()

    async with lock:
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert not entered.is_set()
    await task
    result = stats.snapshot()["stages"]
    assert result["catalog_lock_wait"]["max_seconds"] >= 0.01
    assert result["catalog_lock_hold"]["count"] == 2
    assert not lock.locked()
