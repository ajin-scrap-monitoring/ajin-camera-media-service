import asyncio
import json
import threading
import time

import pytest

from camera_media_service.loop_watchdog import LoopWatchdog


@pytest.mark.asyncio
async def test_watchdog_captures_blocked_main_stack_without_locals_and_stops():
    watchdog = LoopWatchdog(threshold=0.04, interval=0.005)
    sensitive_local = "must-not-appear-in-diagnostics"
    watchdog.start()
    try:
        # The independent watcher must see the actual blocked event-loop thread.
        time.sleep(0.15)
        sample = watchdog.heartbeat()
        assert sample is not None
        assert sample["heartbeat_age_seconds"] >= 0.04
        assert any(
            frame["function"]
            == "test_watchdog_captures_blocked_main_stack_without_locals_and_stops"
            for frame in sample["frames"]
        )
        assert len(sample["frames"]) <= 12
        assert sensitive_local not in json.dumps(sample)
        assert all(set(frame) == {"file", "function", "line"} for frame in sample["frames"])
        assert watchdog.heartbeat() is None
    finally:
        await watchdog.close()
    assert not watchdog.thread.is_alive()


@pytest.mark.asyncio
async def test_watchdog_repeated_close_cancellation_drains_thread(monkeypatch):
    watchdog = LoopWatchdog(threshold=0.1, interval=0.005)
    watchdog.start()
    entered, release = threading.Event(), threading.Event()
    original = watchdog.thread.join

    def blocked_join():
        entered.set()
        assert release.wait(3)
        original()

    monkeypatch.setattr(watchdog.thread, "join", blocked_join)
    closing = asyncio.create_task(watchdog.close())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        closing.cancel()
        await asyncio.sleep(0.01)
        closing.cancel()
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not watchdog.thread.is_alive()
        await watchdog.close()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await watchdog.close()
