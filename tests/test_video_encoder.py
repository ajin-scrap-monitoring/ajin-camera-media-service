import asyncio
from pathlib import Path

import imageio_ffmpeg
import pytest
from test_daily_recording import jpeg

from camera_media_service import encoding
from camera_media_service.daily_models import StoredFrame
from camera_media_service.settings import Settings, SettingsError


@pytest.mark.asyncio
async def test_service_attaches_encoder_diagnostics_without_gpu_required(tmp_path):
    from camera_media_service.pipeline import LiveFrameHub
    from camera_media_service.recording import RecordingService
    from camera_media_service.store import LatestFrameStore

    service = RecordingService(
        tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()), video_encoder="auto"
    )
    try:
        assert service.video_encoder.diagnostics is service.diagnostics
        assert service.video_encoder.backend is None
    finally:
        await service.close()


def test_encoder_setting_is_opt_in_and_validated():
    env = {"EDGE_AUTH_SECRET": '{"cam":"test"}'}
    assert getattr(Settings.from_env(env), "video_encoder", None) == "cpu"
    assert Settings.from_env({**env, "MEDIA_VIDEO_ENCODER": "auto"}).video_encoder == "auto"
    with pytest.raises(SettingsError, match="MEDIA_VIDEO_ENCODER"):
        Settings.from_env({**env, "MEDIA_VIDEO_ENCODER": "surprise"})


def fixture_frames(tmp_path):
    buffer = tmp_path / "buffer"
    buffer.mkdir()
    frames = []
    for i in range(30):
        path = buffer / f"{i}.jpg"
        path.write_bytes(jpeg())
        frames.append(StoredFrame(path.name, 1000 + i / 30, path.stat().st_size))
    return buffer, frames


@pytest.mark.asyncio
async def test_missing_gpu_is_probed_once_and_cpu_outputs_are_verified(tmp_path, monkeypatch):
    from camera_media_service.video_encoder import H264Encoder

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoder = H264Encoder(ffmpeg, "auto")
    buffer, frames = fixture_frames(tmp_path)
    original = encoding.start_media_process
    commands = []

    async def unavailable(*args, **kwargs):
        commands.append(args)
        if "h264_nvenc" in args:
            args = tuple("missing_hardware_encoder" if a == "h264_nvenc" else a for a in args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(encoding, "start_media_process", unavailable)
    for i in range(2):
        result = await encoding.encode_daily_segment(
            ffmpeg,
            buffer,
            tmp_path / f"{i}.ts",
            frames,
            {"width": 64, "height": 48, "fps": 15, "_encoder": encoder},
        )
        assert result["media_info"]["codec"] == "h264"
        assert result["media_info"]["fps"] == 15
        assert result["end_at"] == 1001
    assert encoder.backend == "libx264"
    assert sum("h264_nvenc" in c for c in commands) == 1
    assert len(list(buffer.glob("*.jpg"))) == 30


class CompletedProbe:
    returncode = 0

    async def communicate(self):
        return b"", b""


@pytest.mark.asyncio
async def test_gpu_job_failure_retries_same_source_with_cpu_before_publication(
    tmp_path,
    monkeypatch,
):
    from camera_media_service.video_encoder import H264Encoder

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoder = H264Encoder(ffmpeg, "auto")
    buffer, frames = fixture_frames(tmp_path)
    original = encoding.start_media_process
    manifests = []

    async def gpu_fails(*args, **kwargs):
        if "lavfi" in args:
            return CompletedProbe()
        if "concat" in args:
            manifests.append(Path(args[args.index("-i") + 1]).read_bytes())
        if "h264_nvenc" in args:
            args = tuple("missing_hardware_encoder" if a == "h264_nvenc" else a for a in args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(encoding, "start_media_process", gpu_fails)
    result = await encoding.encode_daily_segment(
        ffmpeg,
        buffer,
        tmp_path / "daily.ts",
        frames,
        {"width": 64, "height": 48, "fps": 15, "_encoder": encoder},
    )
    assert len(manifests) == 2 and manifests[0] == manifests[1]
    assert encoder.backend == "libx264" and encoder.fallbacks == 1
    assert result["duration"] == 1 and result["end_at"] == 1001
    assert (tmp_path / "daily.ts").exists()
    assert not list(tmp_path.rglob("*.tmp.ts"))


@pytest.mark.asyncio
async def test_failed_gpu_verification_uses_cpu_and_caches_fallback():
    from camera_media_service.video_encoder import H264Encoder

    encoder = H264Encoder("ffmpeg", "auto")
    calls, verified = [], []

    async def start(*args, **kwargs):
        calls.append(args)
        return CompletedProbe()

    async def verify():
        verified.append(True)
        if len(verified) == 1:
            raise RuntimeError("Media verification failed")
        return {"verified": True}

    result = await encoder.encode(
        ("ffmpeg", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "out.ts"),
        verify,
        timeout=1,
        start=start,
    )
    assert result == {"verified": True}
    assert len(calls) == 3 and "h264_nvenc" in calls[1] and "libx264" in calls[2]
    assert encoder.backend == "libx264" and encoder.fallbacks == 1


@pytest.mark.asyncio
async def test_cpu_mode_never_probes_gpu_and_propagates_bad_source():
    from camera_media_service.video_encoder import H264Encoder

    encoder = H264Encoder("ffmpeg", "cpu")
    calls = []

    async def start(*args, **kwargs):
        calls.append(args)
        return CompletedProbe()

    async def verify():
        raise RuntimeError("bad source")

    with pytest.raises(RuntimeError, match="bad source"):
        await encoder.encode(("ffmpeg", "-c:v", "libx264"), verify, timeout=1, start=start)
    assert len(calls) == 1 and "lavfi" not in calls[0]


@pytest.mark.asyncio
async def test_cancelled_gpu_probe_reaps_child_and_does_not_retry_cpu():
    from test_media_process_cancellation import DelayedChild

    from camera_media_service.video_encoder import H264Encoder

    encoder = H264Encoder("ffmpeg", "auto")
    child = DelayedChild()
    calls, started = [], asyncio.Event()

    async def start(*args, **kwargs):
        calls.append(args)
        started.set()
        return child

    async def verify():
        pytest.fail("Cancelled probe must not encode/publish")

    task = asyncio.create_task(encoder.encode(("ffmpeg",), verify, timeout=10, start=start))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(child.reaping.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        child.release_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.killed and child.reaped and len(calls) == 1
    finally:
        child.release_reap.set()
        await asyncio.gather(task, return_exceptions=True)
