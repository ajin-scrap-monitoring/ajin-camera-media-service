"""Linux-only isolated CPU/NVENC comparison; never use an existing recording store."""

import argparse
import asyncio
import json
import re
import resource
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from camera_media_service import encoding
from camera_media_service.daily_models import StoredFrame
from camera_media_service.video_encoder import H264Encoder
from tools.recording_priority_probe import images


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr[-2500:])
    return result.stderr


def quality(ffmpeg, reference, output, *, seek=0, seconds=10):
    metrics = {}
    for name in ("ssim", "psnr"):
        text = run(
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-threads",
            "1",
            "-ss",
            str(seek),
            "-i",
            str(reference),
            "-threads",
            "1",
            "-i",
            str(output),
            "-lavfi",
            f"[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]{name}=shortest=1",
            "-t",
            str(seconds),
            "-an",
            "-f",
            "null",
            "-",
        )
        expression = r"All:([0-9.]+)" if name == "ssim" else r"average:([0-9.]+|inf)"
        values = re.findall(expression, text)
        if not values:
            raise RuntimeError("Missing quality measurement: " + text[-1000:])
        metrics[name] = float(values[-1])
    return metrics


async def compare(args):
    root = args.root.resolve()
    if root.exists() or not root.name.startswith("gpu-probe-"):
        raise ValueError("Use a new isolated gpu-probe-* root")
    root.mkdir(parents=True)
    buffer = root / "buffer"
    buffer.mkdir()
    ffmpeg = args.ffmpeg
    if args.jpeg_dir:
        originals = sorted(args.jpeg_dir.glob("*.jpg"))
        if len(originals) != 300:
            raise ValueError("JPEG fixture must contain exactly 300 ordered files")
        for i, source in enumerate(originals):
            shutil.copyfile(source, buffer / f"{i:06d}.jpg")
    elif args.source:
        run(
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(args.source),
            "-an",
            "-vf",
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30",
            "-q:v",
            "3",
            "-frames:v",
            "300",
            str(buffer / "%06d.jpg"),
        )
    else:
        source = images()
        for i in range(300):
            (buffer / f"{i:06d}.jpg").write_bytes(source[i % len(source)])
    paths = sorted(buffer.glob("*.jpg"))
    if len(paths) != 300:
        raise RuntimeError("Source must supply at least 10 seconds")
    frames = [StoredFrame(p.name, 1000 + i / 30, p.stat().st_size) for i, p in enumerate(paths)]
    cpu, gpu = H264Encoder(ffmpeg), H264Encoder(ffmpeg, "auto")
    probe_started = time.monotonic()
    await gpu._select(encoding.start_media_process)
    probe_seconds = time.monotonic() - probe_started
    if gpu.backend != "h264_nvenc":
        raise RuntimeError("Actual NVENC probe failed; comparison must not disguise CPU fallback")
    captured = []
    original = encoding.start_media_process

    async def capture(*command, **kwargs):
        if "concat" in command and not captured:
            manifest = Path(command[command.index("-i") + 1])
            captured.append(manifest.read_text())
        return await original(*command, **kwargs)

    results = []
    encoding.start_media_process = capture
    profile = {"width": 1920, "height": 1080, "fps": 15}
    try:
        for i, order in enumerate((("cpu", "gpu"), ("gpu", "cpu"), ("cpu", "gpu"))):
            for mode in order:
                selected = gpu if mode == "gpu" else cpu
                before = [
                    resource.getrusage(k) for k in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)
                ]
                began = time.monotonic()
                metadata = await encoding.encode_daily_segment(
                    ffmpeg,
                    buffer,
                    root / f"{mode}-{i}.ts",
                    frames,
                    {**profile, "_encoder": selected},
                )
                wall = time.monotonic() - began
                after = [
                    resource.getrusage(k) for k in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)
                ]
                cpu_seconds = sum(
                    (a.ru_utime + a.ru_stime) - (b.ru_utime + b.ru_stime)
                    for a, b in zip(after, before, strict=True)
                )
                assert metadata["media_info"]["fps"] == 15
                assert metadata["start_at"] == 1000 and metadata["end_at"] == 1010
                assert abs(metadata["duration"] - 10) < 0.01
                assert selected.backend == ("h264_nvenc" if mode == "gpu" else "libx264")
                result = {
                    "mode": mode,
                    "iteration": i,
                    "backend": selected.backend,
                    "cpu_seconds": cpu_seconds,
                    "wall_seconds": wall,
                    "bytes": metadata["size_bytes"],
                    "profile": metadata["media_info"],
                }
                results.append(result)
                print(json.dumps(result), flush=True)
    finally:
        encoding.start_media_process = original
    manifest = buffer / "reference.ffconcat"
    manifest.write_text(captured[0])
    reference = root / "reference.mkv"
    run(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(manifest),
        "-an",
        "-vf",
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-r",
        "15",
        "-fps_mode",
        "cfr",
        "-frames:v",
        "150",
        "-c:v",
        "ffv1",
        "-threads",
        "2",
        str(reference),
    )
    measured = {mode: quality(ffmpeg, reference, root / f"{mode}-0.ts") for mode in ("cpu", "gpu")}
    summaries = {
        mode: {
            key: statistics.median(r[key] for r in results if r["mode"] == mode)
            for key in ("cpu_seconds", "wall_seconds", "bytes")
        }
        for mode in ("cpu", "gpu")
    }
    segments = [
        {"path": f"{mode}-{i}.ts", "duration": 10, "profile": profile}
        for mode, i in (("cpu", 0), ("gpu", 0), ("cpu", 1))
    ]
    exports = root / "exports"
    exports.mkdir()
    mixed = exports / "mixed.mp4"
    await encoding.merge_daily_mp4(ffmpeg, segments, exports / "mixed.tmp.mp4", mixed)
    verified = await encoding.verify_archive(ffmpeg, mixed, segments)
    copied_quality = quality(ffmpeg, mixed, root / "gpu-0.ts", seek=10)
    assert copied_quality["ssim"] > 0.9999, "Mixed-encoder archive altered decoded GPU frames"
    clips = root / "clips"
    clips.mkdir()
    event_runs = []
    for i, order in enumerate((("cpu", "gpu"), ("gpu", "cpu"), ("cpu", "gpu"))):
        for mode in order:
            before = [
                resource.getrusage(k) for k in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)
            ]
            began = time.monotonic()
            metadata = await encoding.encode_clip(
                ffmpeg,
                buffer,
                clips,
                f"{mode}-event-{i}",
                [(f.path, f.received_at) for f in frames],
                encoder=gpu if mode == "gpu" else cpu,
            )
            actual_backend = (gpu if mode == "gpu" else cpu).backend
            assert actual_backend == ("h264_nvenc" if mode == "gpu" else "libx264"), (
                "Event comparison cannot measure CPU fallback as GPU encoding"
            )
            wall = time.monotonic() - began
            after = [
                resource.getrusage(k) for k in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)
            ]
            used = sum(
                (a.ru_utime + a.ru_stime) - (b.ru_utime + b.ru_stime)
                for a, b in zip(after, before, strict=True)
            )
            event_runs.append(
                {
                    "mode": mode,
                    "backend": actual_backend,
                    "cpu_seconds": used,
                    "wall_seconds": wall,
                    "bytes": metadata["size_bytes"],
                }
            )
    event = await encoding.probe_media(ffmpeg, clips / "gpu-event-0.mp4")
    await encoding.probe_media(ffmpeg, clips / "cpu-event-0.mp4")
    event_medians = {
        mode: {
            key: statistics.median(r[key] for r in event_runs if r["mode"] == mode)
            for key in ("cpu_seconds", "wall_seconds", "bytes")
        }
        for mode in ("cpu", "gpu")
    }
    event_quality = quality(ffmpeg, clips / "cpu-event-0.mp4", clips / "gpu-event-0.mp4")
    result = {
        "state": "completed",
        "same_input_frames": 300,
        "raw_frames_retained": len(list(buffer.glob("*.jpg"))),
        "scope": "1080p30fps JPEG test input to 1080p15fps; startup probe excluded from CPU timing",
        "source_kind": (
            "provided_JPEGs_on_controlled_30fps_grid"
            if args.jpeg_dir
            else "provided_video_resampled_to_test_JPEGs"
            if args.source
            else "synthetic"
        ),
        "probe_seconds": probe_seconds,
        "medians": summaries,
        "quality": measured,
        "cpu_reduction_percent": 100
        * (1 - summaries["gpu"]["cpu_seconds"] / summaries["cpu"]["cpu_seconds"]),
        "size_ratio_gpu_to_cpu": summaries["gpu"]["bytes"] / summaries["cpu"]["bytes"],
        "mixed_archive": {
            "duration": verified["duration"],
            "frames": verified["packet_count"],
            "gpu_section_copy_ssim": copied_quality["ssim"],
        },
        "gpu_event": {
            "duration": event["duration"],
            "bytes": event["size_bytes"],
            "backend": gpu.backend,
            "medians": event_medians,
            "quality_relative_to_cpu_output": event_quality,
            "size_ratio_gpu_to_cpu": event_medians["gpu"]["bytes"] / event_medians["cpu"]["bytes"],
            "cpu_reduction_percent": 100
            * (1 - event_medians["gpu"]["cpu_seconds"] / event_medians["cpu"]["cpu_seconds"]),
            "timing_scope": "encode and fast metadata probe; full decode checked separately",
        },
        "runs": results,
    }
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "runs"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--source", type=Path)
    sources.add_argument("--jpeg-dir", type=Path)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    asyncio.run(compare(parser.parse_args()))
