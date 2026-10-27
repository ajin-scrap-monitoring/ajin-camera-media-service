import asyncio
import importlib

import imageio_ffmpeg
import pytest
from test_clip_media import make_ts

from camera_media_service.encoding import merge_daily_mp4


async def prepared(tmp_path):
    path = make_ts(tmp_path)
    segments = [
        {
            "path": str(path.relative_to(tmp_path)),
            "duration": 6.0,
            "profile": {"width": 160, "height": 120, "fps": 10},
        }
    ]
    output = tmp_path / "daily-exports" / "master.mp4"
    await merge_daily_mp4(
        imageio_ffmpeg.get_ffmpeg_exe(), segments, output.with_suffix(".tmp.mp4"), output
    )
    return segments, output


@pytest.mark.asyncio
async def test_archive_verifies_all_packet_count_before_source_deletion(tmp_path):
    segments, path = await prepared(tmp_path)
    verify = importlib.import_module("camera_media_service.encoding").verify_archive
    result = await verify(imageio_ffmpeg.get_ffmpeg_exe(), path, segments)
    assert result["packet_count"] == 60
    assert abs(result["duration"] - 6) < 0.1
    assert result["size_bytes"] == path.stat().st_size
    with pytest.raises(RuntimeError):
        await verify(imageio_ffmpeg.get_ffmpeg_exe(), path, [{**segments[0], "duration": 5.0}])


@pytest.mark.asyncio
async def test_archive_verification_rejects_missing_late_packets(tmp_path):
    segments, path = await prepared(tmp_path)
    verify = importlib.import_module("camera_media_service.encoding").verify_archive
    with path.open("r+b") as f:
        f.truncate(path.stat().st_size // 2)
    with pytest.raises(RuntimeError):
        await verify(imageio_ffmpeg.get_ffmpeg_exe(), path, segments)


@pytest.mark.asyncio
async def test_archive_rejects_corrupt_payload_with_unchanged_count_and_size(tmp_path):
    segments, path = await prepared(tmp_path)
    with path.open("r+b") as file:
        file.seek(-100, 2)
        file.write(bytes(100))
    verify = importlib.import_module("camera_media_service.encoding").verify_archive
    with pytest.raises(RuntimeError):
        await verify(imageio_ffmpeg.get_ffmpeg_exe(), path, segments)


@pytest.mark.asyncio
async def test_verification_cancellation_during_spawn_reaps_child(tmp_path, monkeypatch):
    segments, path = await prepared(tmp_path)
    module = importlib.import_module("camera_media_service.encoding")
    spawn = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(
        module.verify_archive(imageio_ffmpeg.get_ffmpeg_exe(), path, segments)
    )
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
    finally:
        release.set()
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.communicate()
