import asyncio
import threading
from pathlib import Path

import pytest

from camera_media_service import encoding, persisted_media_info
from camera_media_service.daily_models import StoredFrame


async def wait_thread_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_publisher_retains_ownership_until_blocked_rename_finishes(
    tmp_path, monkeypatch, cancellations
):
    temporary, final = tmp_path / "temporary.mp4", tmp_path / "final.mp4"
    temporary.write_bytes(b"verified media")
    metadata = {"_file_identity": persisted_media_info.file_identity(temporary.stat())}
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_replace = persisted_media_info.os.replace

    def replace(source, destination):
        entered.set()
        try:
            assert release.wait(2)
            return real_replace(source, destination)
        finally:
            finished.set()

    monkeypatch.setattr(persisted_media_info.os, "replace", replace)
    task = asyncio.create_task(
        persisted_media_info.publish_generated_info(metadata, temporary, final)
    )
    try:
        await wait_thread_event(entered)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), "publisher released ownership while rename still running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert final.read_bytes() == b"verified media"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await wait_thread_event(finished)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
@pytest.mark.parametrize("kind", ["daily", "clip", "merge"])
async def test_encoder_drains_manifest_write_before_cleanup_and_lease_release(
    tmp_path, monkeypatch, cancellations, kind
):
    buffer = tmp_path / "buffer"
    buffer.mkdir()
    destination = tmp_path / "daily" / "part.ts"
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_write = Path.write_text

    def write(path, *args, **kwargs):
        if path.suffix != ".ffconcat":
            return real_write(path, *args, **kwargs)
        entered.set()
        try:
            assert release.wait(2)
            return real_write(path, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(Path, "write_text", write)
    profile = {"width": 32, "height": 32, "fps": 30}
    if kind == "daily":
        coroutine = encoding.encode_daily_segment(
            "must-not-start", buffer, destination, [StoredFrame("frame.jpg", 1000, 5)], profile
        )
    elif kind == "clip":
        output = tmp_path / "clips"
        output.mkdir()
        destination = output / "event.mp4"
        coroutine = encoding.encode_clip(
            "must-not-start", buffer, output, "event", [("frame.jpg", 1000)]
        )
    else:
        destination = tmp_path / "archive" / "day.mp4"
        coroutine = encoding.merge_daily_mp4(
            "must-not-start",
            [{"path": "daily/source.ts", "duration": 1, "profile": profile}],
            destination.with_suffix(".tmp.mp4"),
            destination,
        )
    task = asyncio.create_task(coroutine)
    try:
        await wait_thread_event(entered)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), (
                "encoder released frame lease while manifest writer still running"
            )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert not list(tmp_path.rglob("*.ffconcat"))
        assert not destination.exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await wait_thread_event(finished)
