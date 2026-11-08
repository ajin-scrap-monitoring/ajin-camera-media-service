import asyncio
import threading

import pytest
from test_recording import make_service

from camera_media_service.persisted_media_info import file_identity


def source(root, name):
    path = root / name
    path.write_bytes(b"identity-attested generated media")
    return path, {
        "_file_identity": file_identity(path.stat()),
        "media_info": {"status": "ready", "codec": "h264"},
    }


@pytest.mark.asyncio
async def test_optional_daily_cache_is_bounded_and_deduplicated(tmp_path, monkeypatch):
    import camera_media_service.daily_metadata as module

    service = make_service(tmp_path)
    writer = module.DailyMetadataWriter(service, max_pending=2)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked(recording, path, metadata):
        calls.append(path)
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(module, "persist_generated_info", blocked)
    sources = [source(tmp_path, f"{i}.ts") for i in range(4)]
    try:
        assert writer.offer(*sources[0])
        await asyncio.wait_for(entered.wait(), 2)
        assert writer.offer(*sources[0])  # Same identity does not allocate another task.
        assert writer.offer(*sources[1]) and writer.offer(*sources[2])
        assert not writer.offer(*sources[3])
        assert len(calls) == 1
        release.set()
        await asyncio.wait_for(writer.join(), 3)
        assert calls == [p for p, _ in sources[:3]]
        assert service.diagnostics.loss_counts["metadata_cache_full"] == 1
    finally:
        release.set()
        await writer.close()
        await service.close()


@pytest.mark.asyncio
async def test_daily_cache_skips_deleted_or_replaced_sources(tmp_path, monkeypatch):
    import camera_media_service.daily_metadata as module

    service = make_service(tmp_path)
    writer = module.DailyMetadataWriter(service)
    service.queue = asyncio.Queue(1200)
    for _ in range(80):
        service.queue.put_nowait(None)
    calls = []

    async def persist(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(module, "persist_generated_info", persist)
    first, first_info = source(tmp_path, "deleted.ts")
    second, second_info = source(tmp_path, "replaced.ts")
    try:
        assert writer.offer(first, first_info) and writer.offer(second, second_info)
        await asyncio.sleep(0.03)
        assert not calls  # Foreground pressure defers optional I/O.
        first.unlink()
        second.write_bytes(b"different generation")
        service.queue = None
        await asyncio.wait_for(writer.join(), 3)
        assert not calls
        assert service.diagnostics.loss_counts["metadata_cache_stale"] == 2
    finally:
        service.queue = None
        await writer.close()
        await service.close()


@pytest.mark.asyncio
async def test_daily_cache_close_drains_only_inflight_commit_and_discards_queue(
    tmp_path, monkeypatch
):
    import camera_media_service.daily_metadata as module
    import camera_media_service.persisted_media_info as persisted

    service = make_service(tmp_path)
    writer = module.DailyMetadataWriter(service)
    entered, release = threading.Event(), threading.Event()
    original = persisted._optional_write
    calls = []

    def blocked(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(persisted, "_optional_write", blocked)
    closing = None
    try:
        assert writer.offer(*source(tmp_path, "first.ts"))
        assert await asyncio.to_thread(entered.wait, 2)
        assert writer.offer(*source(tmp_path, "second.ts"))
        closing = asyncio.create_task(writer.close())
        await asyncio.sleep(0.03)
        assert not closing.done()
        assert not writer.offer(*source(tmp_path, "third.ts"))
        release.set()
        await asyncio.wait_for(closing, 3)
        await asyncio.wait_for(writer.join(), 1)
        assert len(calls) == 1
    finally:
        release.set()
        if closing:
            await asyncio.gather(closing, return_exceptions=True)
        await writer.close()
        await service.close()


@pytest.mark.asyncio
async def test_recording_owner_closes_its_optional_daily_cache_worker(tmp_path):
    from camera_media_service.daily_metadata import DailyMetadataWriter

    service = make_service(tmp_path)
    writer = DailyMetadataWriter(service)
    try:
        assert writer.offer(*source(tmp_path, "owned.ts"))
        await asyncio.wait_for(writer.join(), 3)
        await service.close()
        assert writer.closed
        assert writer.task.done()
    finally:
        await writer.close()
        await service.close()
