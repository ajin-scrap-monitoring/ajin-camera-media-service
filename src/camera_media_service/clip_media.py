"""Keyframe-bounded stream copying of already encoded recordings."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from pathlib import Path

from camera_media_service.encoding import probe_media, start_media_process
from camera_media_service.recording import ApiError, epoch, iso


async def inspect_keyframes(
    ffmpeg: str, path: Path, *, start: float | None = None, duration: float | None = None
) -> tuple[float, list[float]]:
    window = [] if start is None else ["-ss", str(max(0, start - 1)), "-t", str(duration + 2)]
    process = await start_media_process(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-copyts",
        "-skip_frame",
        "nokey",
        *window,
        "-i",
        str(path),
        "-an",
        "-vf",
        "showinfo",
        "-fps_mode",
        "vfr",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    base = re.search(rb"config in time_base: (\d+)/(\d+)", error)
    ticks = re.findall(rb"\bpts:\s*(-?\d+)\s+pts_time:", error)
    if process.returncode or not base or not ticks:
        raise RuntimeError("Cannot read recording keyframes")
    # pts_time is printed with six significant digits and loses subframe
    # precision late in a full-day file. Integer PTS retains the media timebase.
    times = [int(t) * int(base[1]) / int(base[2]) for t in ticks]
    origin = times[0]
    return origin, [t - origin for t in times]


def snap_archive_start(offset: float, origin: float, keys: list[float]) -> float:
    """Resolve microsecond concat rounding to the segment's actual TS keyframe."""
    nearest = min((origin + key for key in keys), key=lambda value: abs(value - offset))
    return nearest if abs(nearest - offset) <= 2 / 90000 + 0.000001 else offset


async def remux_clip(ffmpeg: str, root: Path, slices: list[dict], output: Path) -> dict:
    if not slices or len({json.dumps(s["profile"], sort_keys=True) for s in slices}) != 1:
        raise ApiError(409, "EXPORT_INCOMPATIBLE_MEDIA", "Recording profiles differ.")
    lines = ["ffconcat version 1.0"]
    intervals, duration = [], 0.0
    inspection = {}
    for item in slices:
        path = (root / item["path"]).resolve()
        archive = "media_start" in item
        owned = root / ("daily-exports" if archive else "daily")
        if not path.is_relative_to(owned.resolve()):
            raise OSError("Recording path is outside daily storage")
        cache_key = (path, item.get("media_start"))
        if cache_key not in inspection:
            inspection[cache_key] = await inspect_keyframes(
                ffmpeg, path, start=item.get("media_start"), duration=item["duration"]
            )
        origin, keys = inspection[cache_key]
        source_start = snap_archive_start(item["media_start"], origin, keys) if archive else origin
        # CFR rounds the last frame; it does not stretch the recording's time axis.
        target_start = source_start + item["clip_start"] - item["start_at"] - origin
        possible = [key for key in keys if key <= target_start + 0.000001]
        if not possible:
            raise ApiError(409, "RECORDING_BOUNDARY_UNAVAILABLE", "No preceding keyframe.")
        start = max(possible)
        wall_start = item["start_at"] + origin + start - source_start
        if (
            item["clip_start"] - wall_start > 1.000001
            or wall_start < item.get("min_start", item["start_at"]) - 0.000001
        ):
            raise ApiError(409, "RECORDING_BOUNDARY_UNAVAILABLE", "Cut exceeds one-second limit.")
        fps = item["profile"]["fps"]
        target_end = item["clip_end"] - item["start_at"]
        end = (
            source_start
            - origin
            + min(item["duration"], math.ceil(target_end * fps - 0.000001) / fps)
        )
        wall_end = min(item["end_at"], item["start_at"] + origin + end - source_start)
        if wall_end > item.get("max_end", item["end_at"]) + 0.000001:
            # At a known gap or the last available packet, do not invent extra coverage.
            end = source_start + target_end - origin
            wall_end = item["clip_end"]
        if wall_end - item["clip_end"] > 1.000001:
            raise ApiError(409, "RECORDING_BOUNDARY_UNAVAILABLE", "Cut exceeds one-second limit.")
        name = path.as_posix().replace("'", "'\\''")
        lines.extend(
            (
                f"file '{name}'",
                f"inpoint {origin + start:.9f}",
                f"outpoint {origin + end:.9f}",
                f"duration {end - start:.9f}",
            )
        )
        intervals.append(
            {
                "start_at": iso(wall_start),
                "end_at": iso(wall_end),
                "playback_start_seconds": round(duration, 6),
                "playback_end_seconds": round(duration + end - start, 6),
            }
        )
        duration += end - start
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = output.with_suffix(".ffconcat")
    temporary = output.with_suffix(".tmp.mp4")
    process = None
    try:
        await asyncio.to_thread(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
        process = await start_media_process(
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
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), timeout=120)
        if process.returncode:
            raise RuntimeError(error[-1500:].decode(errors="replace"))
        metadata = await probe_media(ffmpeg, temporary, full_decode=False)
        if abs(metadata["duration"] - duration) > max(0.15, len(slices) / fps):
            raise RuntimeError("Extracted media duration differs from selected recording")
        # Timestamp strings above are also the public per-interval playback mapping.
        metadata.update(
            start_at=epoch(intervals[0]["start_at"]),
            end_at=epoch(intervals[-1]["end_at"]),
            available_intervals=intervals,
        )
        await asyncio.to_thread(os.replace, temporary, output)
        return metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
