"""Encode pinned JPEG frames; no continuous transcoding of the live stream."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import statistics
import uuid
from pathlib import Path

from camera_media_service.daily_models import StoredFrame


async def encode_clip(
    ffmpeg: str, buffer: Path, output: Path, clip_id: str, frames: list[tuple[str, float]]
) -> None:
    manifest = output / f"{clip_id}.ffconcat"
    temporary = output / f"{clip_id}.tmp.mp4"
    final = output / f"{clip_id}.mp4"
    # Relative, generated UUID names avoid embedding arbitrary paths in ffconcat syntax.
    lines = ["ffconcat version 1.0"]
    for i, (name, timestamp) in enumerate(frames):
        duration = max(0.001, frames[i + 1][1] - timestamp) if i + 1 < len(frames) else 0.033333
        lines.extend(
            (f"file '../buffer/{name}'", "option framerate 1000", f"duration {duration:.6f}")
        )
    lines.extend((f"file '../buffer/{frames[-1][0]}'", "option framerate 1000"))
    await asyncio.to_thread(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-an",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-threads",
            "2",
            "-fps_mode",
            "vfr",
            "-movflags",
            "+faststart",
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), timeout=900)
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg encoding failed: {error[-2000:].decode(errors='replace')}")
        await asyncio.to_thread(os.replace, temporary, final)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


async def probe_media(ffmpeg: str, path: Path, *, full_decode: bool = True) -> dict:
    """Read actual container duration and validate every video packet by decoding."""
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-progress",
        "pipe:1",
        "-nostats",
        *([] if full_decode else ["-frames:v", "3"]),
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        progress, error = await asyncio.wait_for(process.communicate(), timeout=120)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise
    text = error.decode(errors="replace")
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if process.returncode or not match or "Video: h264" not in text:
        raise RuntimeError("Media verification failed: " + text[-1500:])
    times = re.findall(rb"out_time_us=(\d+)", progress)
    duration = (
        (int(times[-1]) / 1_000_000 if times else 0)
        if full_decode
        else (int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]))
    )
    if duration <= 0:
        raise RuntimeError("Empty video")
    return {"duration": duration, "size_bytes": path.stat().st_size}


async def encode_daily_segment(
    ffmpeg: str, buffer: Path, destination: Path, frames: list[StoredFrame], profile: dict
) -> dict:
    if not frames:
        raise ValueError("No frames to encode")
    fps = profile["fps"]
    deltas = [
        b.received_at - a.received_at
        for a, b in zip(frames, frames[1:], strict=False)
        if 0 < b.received_at - a.received_at <= 1
    ]
    tail = statistics.median(deltas) if deltas else 1 / fps
    end_at = min(frames[-1].received_at + tail, profile.get("end_at", float("inf")))
    duration = max(0.001, end_at - frames[0].received_at)
    manifest = buffer / f"{uuid.uuid4()}.ffconcat"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.ts")
    lines = ["ffconcat version 1.0"]
    for i, frame in enumerate(frames):
        delta = (
            frames[i + 1].received_at - frame.received_at
            if i + 1 < len(frames)
            else end_at - frame.received_at
        )
        lines.extend(
            (f"file '{frame.path}'", "option framerate 1000", f"duration {max(0.001, delta):.6f}")
        )
    lines.extend((f"file '{frames[-1].path}'", "option framerate 1000"))
    await asyncio.to_thread(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
    width, height = profile["width"], profile["height"]
    filters = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-an",
            "-vf",
            filters,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-threads",
            "2",
            "-bf",
            "0",
            "-g",
            str(fps * 10),
            "-sc_threshold",
            "0",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            "-frames:v",
            str(max(1, math.ceil(duration * fps - 0.000001))),
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), timeout=120)
        if process.returncode:
            raise RuntimeError(error[-1500:].decode(errors="replace"))
        metadata = await probe_media(ffmpeg, temporary)
        metadata.update(
            start_at=frames[0].received_at,
            end_at=end_at,
            profile={k: profile[k] for k in ("width", "height", "fps")},
        )
        # Persist exact coverage before rename; a crash before the DB commit is recoverable.
        prepared = destination.with_suffix(".prepared.json")

        def prepare():
            with prepared.open("w", encoding="utf-8") as file:
                json.dump(metadata, file)
                file.flush()
                os.fsync(file.fileno())

        await asyncio.to_thread(prepare)
        await asyncio.to_thread(os.replace, temporary, destination)
        return metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


async def merge_daily_mp4(ffmpeg: str, segments: list[dict], temporary: Path, final: Path) -> dict:
    if not segments or len({json.dumps(s["profile"], sort_keys=True) for s in segments}) != 1:
        raise RuntimeError("EXPORT_INCOMPATIBLE_MEDIA")
    final.parent.mkdir(parents=True, exist_ok=True)
    manifest = final.with_suffix(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for segment in segments:
        path = Path(segment["path"]).as_posix()
        lines.extend((f"file '../{path}'", f"duration {segment['duration']:.6f}"))
    await asyncio.to_thread(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-xerror",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-an",
            "-c:v",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), timeout=3600)
        if process.returncode:
            raise RuntimeError(error[-1500:].decode(errors="replace"))
        metadata = await probe_media(ffmpeg, temporary, full_decode=False)
        if abs(metadata["duration"] - sum(s["duration"] for s in segments)) > max(
            0.1, len(segments) / 90000
        ):
            raise RuntimeError("Export duration differs from saved media")
        await asyncio.to_thread(os.replace, temporary, final)
        return metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
