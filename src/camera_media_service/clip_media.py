"""Keyframe-bounded stream copying of already encoded recordings."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from pathlib import Path

from camera_media_service.encoding import probe_media
from camera_media_service.recording import ApiError, epoch, iso


async def inspect_keyframes(ffmpeg: str, path: Path) -> tuple[float, list[float]]:
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-copyts",
        "-skip_frame",
        "nokey",
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
    times = [float(t) for t in re.findall(rb"pts_time:([-+\d.e]+)", error)]
    if process.returncode or not times:
        raise RuntimeError("Cannot read recording keyframes")
    origin = times[0]
    return origin, [t - origin for t in times]


async def remux_clip(ffmpeg: str, root: Path, slices: list[dict], output: Path) -> dict:
    if not slices or len({json.dumps(s["profile"], sort_keys=True) for s in slices}) != 1:
        raise ApiError(409, "EXPORT_INCOMPATIBLE_MEDIA", "Recording profiles differ.")
    lines = ["ffconcat version 1.0"]
    intervals, duration = [], 0.0
    inspection = {}
    for item in slices:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to((root / "daily").resolve()):
            raise OSError("Recording path is outside daily storage")
        if path not in inspection:
            inspection[path] = await inspect_keyframes(ffmpeg, path)
        origin, keys = inspection[path]
        # CFR rounds the last frame; it does not stretch the recording's time axis.
        target_start = item["clip_start"] - item["start_at"]
        possible = [key for key in keys if key <= target_start + 0.000001]
        if not possible:
            raise ApiError(409, "RECORDING_BOUNDARY_UNAVAILABLE", "No preceding keyframe.")
        start = max(possible)
        wall_start = item["start_at"] + start
        if (
            item["clip_start"] - wall_start > 1.000001
            or wall_start < item.get("min_start", item["start_at"]) - 0.000001
        ):
            raise ApiError(409, "RECORDING_BOUNDARY_UNAVAILABLE", "Cut exceeds one-second limit.")
        fps = item["profile"]["fps"]
        target_end = item["clip_end"] - item["start_at"]
        end = min(item["duration"], math.ceil(target_end * fps - 0.000001) / fps)
        wall_end = min(item["end_at"], item["start_at"] + end)
        if wall_end > item.get("max_end", item["end_at"]) + 0.000001:
            # At a known gap or the last available packet, do not invent extra coverage.
            end = target_end
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
