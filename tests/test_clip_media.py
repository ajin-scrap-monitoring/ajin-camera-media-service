import importlib
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from camera_media_service.recording import ApiError


def make_ts(root: Path, *, seconds=6, gop=10):
    path = root / "daily" / "fixture.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=160x120:rate=10:duration={seconds}",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-bf",
            "0",
            "-g",
            str(gop),
            "-sc_threshold",
            "0",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.mark.asyncio
async def test_fractional_stream_copy_cut_is_decodable_and_within_one_second(tmp_path):
    module = importlib.import_module("camera_media_service.clip_media")
    path = make_ts(tmp_path)
    output = tmp_path / "clips" / "result.mp4"
    output.parent.mkdir()
    slices = [
        {
            "path": str(path.relative_to(tmp_path)),
            "start_at": 1000.0,
            "end_at": 1006.0,
            "clip_start": 1001.35,
            "clip_end": 1004.25,
            "duration": 6.0,
            "profile": {"width": 160, "height": 120, "fps": 10},
        }
    ]
    metadata = await module.remux_clip(imageio_ffmpeg.get_ffmpeg_exe(), tmp_path, slices, output)
    assert 1000.35 <= metadata["start_at"] <= 1001.35
    assert 1004.25 <= metadata["end_at"] <= 1005.25
    assert 2.9 <= metadata["duration"] <= 4.9
    assert output.is_file()
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-xerror",
            "-i",
            str(output),
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    assert not list(output.parent.glob("*.ffconcat"))
    assert not list(output.parent.glob("*.tmp.mp4"))


@pytest.mark.asyncio
async def test_old_long_gop_cannot_silently_expand_a_cut_by_more_than_one_second(tmp_path):
    module = importlib.import_module("camera_media_service.clip_media")
    path = make_ts(tmp_path, seconds=12, gop=100)
    output = tmp_path / "result.mp4"
    with pytest.raises(ApiError) as error:
        await module.remux_clip(
            imageio_ffmpeg.get_ffmpeg_exe(),
            tmp_path,
            [
                {
                    "path": str(path.relative_to(tmp_path)),
                    "start_at": 1000.0,
                    "end_at": 1012.0,
                    "clip_start": 1005.5,
                    "clip_end": 1008.0,
                    "duration": 12.0,
                    "profile": {"width": 160, "height": 120, "fps": 10},
                }
            ],
            output,
        )
    assert error.value.code == "RECORDING_BOUNDARY_UNAVAILABLE"
    assert not output.exists()


@pytest.mark.asyncio
async def test_remux_keeps_actual_wall_clock_gap_and_rejects_profile_mismatch(tmp_path):
    module = importlib.import_module("camera_media_service.clip_media")
    path = make_ts(tmp_path)
    profile = {"width": 160, "height": 120, "fps": 10}
    slices = [
        {
            "path": str(path.relative_to(tmp_path)),
            "start_at": start,
            "end_at": start + 6,
            "clip_start": start,
            "clip_end": start + 3,
            "duration": 6,
            "profile": profile,
        }
        for start in (1000.0, 1010.0)
    ]
    result = await module.remux_clip(
        imageio_ffmpeg.get_ffmpeg_exe(), tmp_path, slices, tmp_path / "ok.mp4"
    )
    assert result["start_at"] == 1000.0
    assert result["end_at"] == 1013.0
    assert abs(result["duration"] - 6) < 0.2
    assert len(result["available_intervals"]) == 2
    slices[1] = {**slices[1], "profile": {**profile, "width": 320}}
    with pytest.raises(ApiError) as error:
        await module.remux_clip(
            imageio_ffmpeg.get_ffmpeg_exe(), tmp_path, slices, tmp_path / "bad.mp4"
        )
    assert error.value.code == "EXPORT_INCOMPATIBLE_MEDIA"


@pytest.mark.asyncio
async def test_daily_encoder_produces_one_second_keyframes(tmp_path):
    from test_daily_recording import jpeg

    from camera_media_service.daily_models import StoredFrame
    from camera_media_service.encoding import encode_daily_segment

    buffer = tmp_path / "buffer"
    buffer.mkdir()
    frame = buffer / "frame.jpg"
    frame.write_bytes(jpeg())
    frames = [StoredFrame(frame.name, 1000 + i / 10, frame.stat().st_size) for i in range(40)]
    target = tmp_path / "daily" / "fixture.ts"
    await encode_daily_segment(
        imageio_ffmpeg.get_ffmpeg_exe(),
        buffer,
        target,
        frames,
        {"width": 320, "height": 240, "fps": 10, "end_at": 1004.0},
    )
    # showinfo exposes actual IDR/keyframe positions in the generated recording.
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-skip_frame",
            "nokey",
            "-i",
            str(target),
            "-vf",
            "showinfo",
            "-fps_mode",
            "vfr",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    import re

    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    assert len(times) >= 4
    assert all(b - a <= 1.001 for a, b in zip(times, times[1:], strict=False))


@pytest.mark.asyncio
async def test_cfr_rounded_tail_does_not_move_start_after_event(tmp_path):
    module = importlib.import_module("camera_media_service.clip_media")
    path = make_ts(tmp_path, seconds=6)
    # The real daily encoder rounds 5.95 seconds to 60 frames at 10fps.
    item = {
        "path": str(path.relative_to(tmp_path)),
        "start_at": 1000.0,
        "end_at": 1005.95,
        "duration": 6.0,
        "clip_start": 1004.990,
        "clip_end": 1005.8,
        "profile": {"width": 160, "height": 120, "fps": 10},
    }
    result = await module.remux_clip(
        imageio_ffmpeg.get_ffmpeg_exe(), tmp_path, [item], tmp_path / "rounded.mp4"
    )
    assert result["start_at"] == 1004.0
    assert result["start_at"] <= 1004.990
    assert abs(result["duration"] - 1.8) < 0.1
