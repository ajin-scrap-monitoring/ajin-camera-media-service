import asyncio
import os
from collections import Counter
from types import SimpleNamespace

import pytest

from camera_media_service import media_details
from camera_media_service.recording import ApiError


def ready(path):
    return {
        "status": "ready",
        "source": "file_probe",
        "codec": "h264",
        "width": int(path.read_text()),
        "height": 120,
        "fps": 10.0,
        "fps_num": 10,
        "fps_den": 1,
        "size_bytes": path.stat().st_size,
    }


def source_files(tmp_path, count):
    paths = [tmp_path / f"{index}.mp4" for index in range(count)]
    for path in paths:
        path.write_text("160")
    return paths


async def wait_admitted(details, count):
    async def wait():
        while details.active != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_index", [None, 0, 7], ids=["capacity", "running", "queued"])
async def test_inspection_bounds_queue_and_processes_and_recovers_cancelled_admission(
    tmp_path, monkeypatch, cancel_index
):
    paths = source_files(tmp_path, 10)
    details = media_details.MediaDetails(SimpleNamespace(ffmpeg="external-probe"))
    release, two_running = asyncio.Event(), asyncio.Event()
    running = peak = 0
    started = []

    async def blocked_inspection(ffmpeg, path):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        started.append(path)
        if running == 2:
            two_running.set()
        try:
            await release.wait()
            return ready(path)
        finally:
            running -= 1

    monkeypatch.setattr(media_details, "inspect_media_info", blocked_inspection)
    tasks = []
    try:
        # Sequential admission makes index 0 running and index 7 queued.
        for index in range(8):
            tasks.append(asyncio.create_task(details.inspect(paths[index])))
            await wait_admitted(details, index + 1)
        await asyncio.wait_for(two_running.wait(), 2)
        with pytest.raises(ApiError) as error:
            await details.inspect(paths[8])
        assert error.value.status == 503
        assert error.value.code == "METADATA_CAPACITY_EXCEEDED"
        assert len(started) == 2

        cancelled = None
        if cancel_index is not None:
            cancelled = tasks[cancel_index]
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            tasks.append(asyncio.create_task(details.inspect(paths[8])))
            await wait_admitted(details, 8)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*(task for task in tasks if task is not cancelled)), 3
        )
        assert len(results) == 8
        assert all(result["status"] == "ready" for result in results)
        assert peak == 2
        assert running == 0
        # Exhausting and draining the queue must leave the next request usable.
        assert (await details.inspect(paths[9]))["width"] == 160
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cache_evicts_oldest_beyond_128_entries_and_keeps_recently_used(
    tmp_path, monkeypatch
):
    paths = source_files(tmp_path, 129)
    details = media_details.MediaDetails(SimpleNamespace(ffmpeg="external-probe"))
    calls = Counter()

    async def inspect(ffmpeg, path):
        calls[path] += 1
        return ready(path)

    monkeypatch.setattr(media_details, "inspect_media_info", inspect)
    for path in paths[:128]:
        await details.inspect(path)
    await details.inspect(paths[0])  # Refresh this entry before overflowing the cache.
    await details.inspect(paths[128])
    await details.inspect(paths[0])
    assert calls[paths[0]] == 1
    await details.inspect(paths[1])
    assert calls[paths[1]] == 2
    assert sum(calls.values()) == 130


@pytest.mark.asyncio
async def test_replaced_file_invalidates_cache_even_with_same_size_and_mtime(tmp_path, monkeypatch):
    path = source_files(tmp_path, 1)[0]
    details = media_details.MediaDetails(SimpleNamespace(ffmpeg="external-probe"))

    async def inspect(ffmpeg, path):
        return ready(path)

    monkeypatch.setattr(media_details, "inspect_media_info", inspect)
    assert (await details.inspect(path))["width"] == 160
    initial = path.stat()
    replacement = tmp_path / "replacement.mp4"
    replacement.write_text("320")
    os.utime(replacement, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    os.replace(replacement, path)
    assert (await details.inspect(path))["width"] == 320


@pytest.mark.asyncio
async def test_transient_unknown_measurement_is_retried_and_cached_results_are_isolated(
    tmp_path, monkeypatch
):
    path = source_files(tmp_path, 1)[0]
    details = media_details.MediaDetails(SimpleNamespace(ffmpeg="external-probe"))
    calls = 0

    async def inspect(ffmpeg, path):
        nonlocal calls
        calls += 1
        if calls == 1:
            return media_details.unknown_media_info("probe_timeout", path.stat().st_size)
        return ready(path)

    monkeypatch.setattr(media_details, "inspect_media_info", inspect)
    assert (await details.inspect(path))["status"] == "unknown"
    measured = await details.inspect(path)
    assert measured["status"] == "ready"
    measured["width"] = 999
    cached = await details.inspect(path)
    assert cached["width"] == 160
    cached["width"] = 888
    assert (await details.inspect(path))["width"] == 160
    assert calls == 2
