import asyncio

import pytest

from camera_media_service import encoding
from camera_media_service.daily_models import StoredFrame


class DelayedChild:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self.reaped = False
        self.reaping = asyncio.Event()
        self.release_reap = asyncio.Event()

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def communicate(self):
        self.reaping.set()
        await self.release_reap.wait()
        self.reaped = True
        return b"", b""


@pytest.mark.asyncio
@pytest.mark.parametrize("second_cancel_during", ["handle_publication", "reaping"])
async def test_repeated_cancellation_keeps_spawn_owner_until_child_is_reaped(
    monkeypatch, second_cancel_during
):
    child = DelayedChild()
    spawned, publish_handle = asyncio.Event(), asyncio.Event()

    async def spawn(*args, **kwargs):
        # The OS child exists here; publishing its asyncio handle is delayed.
        spawned.set()
        await publish_handle.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    owner = asyncio.create_task(encoding.start_media_process("ffmpeg"))
    try:
        await asyncio.wait_for(spawned.wait(), 1)
        owner.cancel()
        await asyncio.sleep(0.01)
        if second_cancel_during == "reaping":
            publish_handle.set()
            await asyncio.wait_for(child.reaping.wait(), 1)
        owner.cancel()
        await asyncio.sleep(0.01)
        assert not owner.done(), (
            "spawn owner ended while OS child still needed retrieval or reaping"
        )
        publish_handle.set()
        await asyncio.wait_for(child.reaping.wait(), 1)
        assert child.killed
        assert not child.reaped
        assert not owner.done()
        child.release_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert child.reaped
    finally:
        publish_handle.set()
        child.release_reap.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clip", "daily"])
async def test_encoder_uses_spawn_owner_when_handle_publication_is_cancelled(
    tmp_path, monkeypatch, kind
):
    child = DelayedChild()
    child.release_reap.set()
    spawned, publish_handle = asyncio.Event(), asyncio.Event()

    async def spawn(*args, **kwargs):
        spawned.set()
        await publish_handle.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    buffer = tmp_path / "buffer"
    buffer.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    if kind == "clip":
        coroutine = encoding.encode_clip("ffmpeg", buffer, output, "event", [("frame.jpg", 1)])
    else:
        coroutine = encoding.encode_daily_segment(
            "ffmpeg",
            buffer,
            output / "segment.ts",
            [StoredFrame("frame.jpg", 1, 5)],
            {"width": 32, "height": 32, "fps": 30},
        )
    owner = asyncio.create_task(coroutine)
    try:
        await asyncio.wait_for(spawned.wait(), 1)
        owner.cancel()
        await asyncio.sleep(0.01)
        owner.cancel()
        await asyncio.sleep(0.01)
        assert not owner.done(), "encoder bypassed protected subprocess handle ownership"
        publish_handle.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert child.killed and child.reaped
        assert not list(tmp_path.rglob("*.ffconcat"))
    finally:
        publish_handle.set()
        await asyncio.gather(owner, return_exceptions=True)
