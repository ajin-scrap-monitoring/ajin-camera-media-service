"""Isolated equal-input CPU probe; never point --root at an existing media store."""

import argparse
import asyncio
import json
import resource
import time
from pathlib import Path

from camera_media_service import encoding
from camera_media_service.daily_models import StoredFrame
from tools.recording_priority_probe import images


async def run(args):
    root = args.root.resolve()
    if not root.name.startswith("efficiency-probe-") or root.exists():
        raise RuntimeError("Use a new, isolated efficiency-probe-* directory")
    root.mkdir(parents=True)
    buffer = root / "buffer"
    buffer.mkdir()
    source = images()
    frames = []
    for i in range(300):
        path = buffer / f"{i}.jpg"
        data = source[i % len(source)]
        path.write_bytes(data)
        frames.append(StoredFrame(path.name, 1000 + i / 30, len(data)))
    manifests = []
    start_process = encoding.start_media_process

    async def capture(*command, **kwargs):
        if "concat" in command:
            path = Path(command[command.index("-i") + 1])
            manifests.append(path.read_text())
        return await start_process(*command, **kwargs)

    encoding.start_media_process = capture
    before_self = resource.getrusage(resource.RUSAGE_SELF)
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    try:
        result = await encoding.encode_daily_segment(
            args.ffmpeg,
            buffer,
            root / "daily.ts",
            frames,
            {"width": 1920, "height": 1080, "fps": 15},
        )
    finally:
        encoding.start_media_process = start_process
    elapsed = time.monotonic() - started
    after_self = resource.getrusage(resource.RUSAGE_SELF)
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)

    def cpu(value):
        return value.ru_utime + value.ru_stime

    inputs = sum(line.startswith("file ") for line in manifests[0].splitlines()) - 1
    assert result["media_info"]["codec"] == "h264"
    assert result["media_info"]["fps"] == 15
    assert result["media_info"]["width"] == 1920 and result["media_info"]["height"] == 1080
    assert abs(result["duration"] - 10) < 0.01 and abs(result["end_at"] - 1010) < 1e-7
    assert len(list(buffer.glob("*.jpg"))) == 300
    proof = {
        "state": "passed",
        "application_module": encoding.__file__,
        "source_jpeg_frames": 300,
        "selected_jpeg_inputs": inputs,
        "source_mean_bytes": sum(f.size_bytes for f in frames) / len(frames),
        "wall_seconds": elapsed,
        "python_cpu_seconds": cpu(after_self) - cpu(before_self),
        "child_cpu_seconds": cpu(after_children) - cpu(before_children),
        "python_peak_rss_kib": after_self.ru_maxrss,
        "child_peak_rss_kib": after_children.ru_maxrss,
        "output_bytes": result["size_bytes"],
        "output_duration": result["duration"],
        "coverage_start": result["start_at"],
        "coverage_end": result["end_at"],
        "profile": result["media_info"],
        "scope": "same_1080p30fps_JPEG_input_to_1080p15fps_H264_including_output_validation",
        "full_source_retained": True,
    }
    args.report.write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    asyncio.run(run(parser.parse_args()))
