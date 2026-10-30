import asyncio
import importlib
import os
import subprocess
import sys

import imageio_ffmpeg
import pytest

from camera_media_service import encoding


def make_video(
    tmp_path, *, width=160, height=120, rate="10", codec="libx264", suffix=".mp4", seconds=0.5
):
    path = tmp_path / ("fixture" + suffix)
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={rate}:duration={seconds}",
            "-c:v",
            codec,
            "-threads",
            "1",
            "-bf",
            "0",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".mp4", ".ts"])
@pytest.mark.parametrize(
    ("width", "height", "rate", "num", "den"),
    [(160, 120, "10", 10, 1), (320, 180, "30000/1001", 30000, 1001)],
)
async def test_inspection_reads_actual_headers_and_exact_stream_rate(
    tmp_path, width, height, rate, num, den, suffix
):
    path = make_video(tmp_path, width=width, height=height, rate=rate, suffix=suffix)
    module = importlib.import_module("camera_media_service.media_info")
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result == {
        "status": "ready",
        "source": "file_probe",
        "codec": "h264",
        "width": width,
        "height": height,
        "fps": num / den,
        "fps_num": num,
        "fps_den": den,
        "size_bytes": path.stat().st_size,
    }


@pytest.mark.asyncio
async def test_inspection_reports_the_file_codec_instead_of_storage_default(tmp_path):
    path = make_video(tmp_path, codec="mpeg4")
    module = importlib.import_module("camera_media_service.media_info")
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["status"] == "ready"
    assert result["codec"] == "mpeg4"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["invalid", "truncated", "missing"])
async def test_unreadable_video_has_unknown_metadata_without_invented_defaults(tmp_path, kind):
    path = tmp_path / "fixture.mp4"
    if kind == "invalid":
        path.write_bytes(b"not a video")
    elif kind == "truncated":
        path = make_video(tmp_path)
        path.write_bytes(path.read_bytes()[:100])
    module = importlib.import_module("camera_media_service.media_info")
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["status"] == "unknown"
    assert result["source"] == "file_probe"
    assert result["reason_code"]
    for field in ("codec", "width", "height", "fps", "fps_num", "fps_den"):
        assert result[field] is None


def test_unparseable_exact_rate_is_not_replaced_with_rounded_summary_rate():
    module = importlib.import_module("camera_media_service.media_info")
    result = module.parse_media_info(
        "Stream #0:0: Video: h264 (High), yuv420p, 320x180, 29.97 fps, 29.97 tbr\n"
        "[Parsed_showinfo_2] config in time_base: 1/30000, frame_rate: 0/0\n"
        "[Parsed_showinfo_2] n: 0 pts: 0 s:320x180 i:P\n",
        123,
    )
    assert result["status"] == "unknown"
    assert result["fps"] is None
    assert result["fps_num"] is None
    assert result["fps_den"] is None


@pytest.mark.asyncio
async def test_probe_returns_measured_metadata_during_existing_decode(tmp_path, monkeypatch):
    path = make_video(tmp_path, width=320, height=180, rate="30000/1001")
    spawn = asyncio.create_subprocess_exec
    children = []

    async def tracked(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    result = await encoding.probe_media(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["media_info"]["width"] == 320
    assert result["media_info"]["height"] == 180
    assert result["media_info"]["fps_num"] == 30000
    assert result["media_info"]["fps_den"] == 1001
    assert len(children) == 1  # Metadata must share validation's existing full-file decode.


@pytest.mark.asyncio
async def test_inspection_decodes_only_initial_frames_and_bounds_showinfo_output(
    tmp_path, monkeypatch
):
    path = make_video(tmp_path, seconds=10)
    module = importlib.import_module("camera_media_service.media_info")
    spawn = asyncio.create_subprocess_exec
    captured = []

    async def observed(*args, **kwargs):
        # Ask the real child for progress solely to observe actual decoded frames.
        child = await spawn(
            *args[:-1],
            "-progress",
            "pipe:1",
            args[-1],
            **{
                **kwargs,
                "stdout": asyncio.subprocess.PIPE,
            },
        )
        communicate = child.communicate

        async def collect(*args, **kwargs):
            result = await communicate(*args, **kwargs)
            captured.append(result)
            return result

        child.communicate = collect
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed)
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["status"] == "ready"
    progress, diagnostics = captured[0]
    assert b"frame=3\n" in progress
    assert diagnostics.count(b" n:") == 1


@pytest.mark.asyncio
async def test_file_removed_during_inspection_returns_unknown(tmp_path, monkeypatch):
    path = make_video(tmp_path)
    module = importlib.import_module("camera_media_service.media_info")
    spawn = asyncio.create_subprocess_exec

    async def removed(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        communicate = child.communicate

        async def collect(*args, **kwargs):
            result = await communicate(*args, **kwargs)
            path.unlink()
            return result

        child.communicate = collect
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", removed)
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["status"] == "unknown"
    assert result["width"] is None


@pytest.mark.asyncio
async def test_file_replaced_during_probe_does_not_keep_old_measurement(tmp_path, monkeypatch):
    path = make_video(tmp_path)
    module = importlib.import_module("camera_media_service.media_info")
    initial = path.stat()
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    spawn = asyncio.create_subprocess_exec

    async def replaced(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        communicate = child.communicate

        async def collect(*args, **kwargs):
            result = await communicate(*args, **kwargs)
            os.replace(replacement, path)
            return result

        child.communicate = collect
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", replaced)
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["status"] == "unknown"
    assert result["reason_code"] == "file_changed"
    assert result["width"] is None


@pytest.mark.asyncio
async def test_archive_verification_returns_measured_metadata(tmp_path):
    path = make_video(tmp_path, width=320, height=180, rate="10")
    result = await encoding.verify_archive(
        imageio_ffmpeg.get_ffmpeg_exe(),
        path,
        [{"duration": 0.5, "profile": {"width": 320, "height": 180, "fps": 10}}],
    )
    assert result["media_info"]["status"] == "ready"
    assert result["media_info"]["width"] == 320
    assert result["media_info"]["fps_num"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("during_spawn", [False, True])
async def test_inspection_cancellation_reaps_child(tmp_path, monkeypatch, during_spawn):
    path = make_video(tmp_path)
    module = importlib.import_module("camera_media_service.media_info")
    spawn = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    children = []

    async def slow(*args, **kwargs):
        child = await spawn(sys.executable, "-c", "import time; time.sleep(120)", **kwargs)
        children.append(child)
        spawned.set()
        if during_spawn:
            await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    task = asyncio.create_task(module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        await asyncio.sleep(0)
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


@pytest.mark.asyncio
async def test_inspection_timeout_reaps_child_and_returns_unknown(tmp_path, monkeypatch):
    path = make_video(tmp_path)
    module = importlib.import_module("camera_media_service.media_info")
    spawn = asyncio.create_subprocess_exec
    children = []

    async def slow(*args, **kwargs):
        child = await spawn(sys.executable, "-c", "import time; time.sleep(120)", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    monkeypatch.setattr(module, "PROBE_TIMEOUT_SECONDS", 0.05)
    result = await module.inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert result["status"] == "unknown"
    assert result["reason_code"] == "probe_timeout"
    assert children[0].returncode is not None
