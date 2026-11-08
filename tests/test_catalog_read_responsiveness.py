import asyncio
import threading
from datetime import date

import pytest
from test_daily_recording import make_daily
from test_frame_packs import frames, service_at

from camera_media_service.daily_models import DailyWindow


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["listing", "detail", "playback"])
async def test_slow_catalogue_read_keeps_receiver_responsive_and_lock_until_worker_drains(
    tmp_path, monkeypatch, operation
):
    clock = [DailyWindow.from_date(date(2026, 11, 8)).start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    key = service.db.execute("SELECT id FROM daily_recordings").fetchone()[0]
    main_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    original = daily.catalog.decode_day

    def blocked(raw):
        assert threading.get_ident() != main_thread, "Catalogue I/O blocks the receiver loop"
        entered.set()
        assert release.wait(5)
        return original(raw)

    monkeypatch.setattr(daily.catalog, "decode_day", blocked)
    call = {
        "listing": lambda: daily.list_days("camera-main", {}, 20, None),
        "detail": lambda: daily.get_day(key),
        "playback": lambda: daily.playback_snapshot(key),
    }[operation]
    task = asyncio.create_task(call())
    try:
        assert await asyncio.to_thread(entered.wait, 1), (
            str(task.exception()) if task.done() else "Worker not entered"
        )
        # Ingest can continue while the same catalogue connection stays exclusively leased.
        assert service.lock.locked()
        await asyncio.sleep(0.01)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and service.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service.lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(daily.catalog, "decode_day", original)
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_slow_pack_accounting_does_not_block_receipts_or_release_connection_on_cancel(
    tmp_path, monkeypatch
):
    service = service_at(tmp_path)
    main_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    original = service.frame_packs.bytes

    def blocked():
        assert threading.get_ident() != main_thread, "Accounting I/O blocks the receiver loop"
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(service.frame_packs, "bytes", blocked)
    task = asyncio.create_task(service.store_frames(frames(30)))
    try:
        assert await asyncio.to_thread(entered.wait, 1), (
            str(task.exception()) if task.done() else "Worker not entered"
        )
        assert service.lock.locked()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and service.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not service.db.execute("SELECT 1 FROM frames").fetchone()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(service.frame_packs, "bytes", original)
        await service.close()
