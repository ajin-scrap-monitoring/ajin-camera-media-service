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

from camera_media_service.background_media import CURRENT, file_input_options
from camera_media_service.daily_models import StoredFrame
from camera_media_service.media_files import storage_io
from camera_media_service.media_info import MEDIA_INFO_FILTER, parse_media_info
from camera_media_service.persisted_media_info import file_identity, publish_generated_info


async def start_media_process(*args, **kwargs):
    """Retain the OS child handle even if shutdown interrupts subprocess creation."""
    control = CURRENT.get()
    if control is not None:
        await control.checkpoint()
    creation = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        child = await asyncio.shield(creation)
        if control is not None:
            control.watch(child)
        return child
    except asyncio.CancelledError:

        async def reap_created_child():
            child = await creation
            if child.returncode is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            await child.communicate()

        cleanup = asyncio.create_task(reap_created_child())
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                # Keep both handle retrieval and pipe draining owned through
                # repeated cancellation, until the OS child has been reaped.
                continue
        raise


async def encode_clip(
    ffmpeg: str, buffer: Path, output: Path, clip_id: str, frames: list[tuple[str, float]]
) -> dict:
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
    process = None
    try:
        await storage_io(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
        process = await start_media_process(
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
        metadata = await probe_media(ffmpeg, temporary, full_decode=False)
        await publish_generated_info(metadata, temporary, final)
        return metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


async def probe_media(ffmpeg: str, path: Path, *, full_decode: bool = True) -> dict:
    """Read actual container duration and validate every video packet by decoding."""
    initial = file_identity(await asyncio.to_thread(path.stat))
    process = await start_media_process(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-vf",
        MEDIA_INFO_FILTER,
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
    final = file_identity(await asyncio.to_thread(path.stat))
    if initial != final:
        raise RuntimeError("Media changed during verification")
    size_bytes = final[2]
    return {
        "duration": duration,
        "size_bytes": size_bytes,
        "media_info": parse_media_info(text, size_bytes),
        "_file_identity": final,
    }


async def encode_daily_segment(
    ffmpeg: str, buffer: Path, destination: Path, frames: list[StoredFrame], profile: dict
) -> dict:
    if not frames:
        raise ValueError("No frames to encode")
    guard = profile.get("_storage_guard")
    if guard is not None:
        await storage_io(guard)
    fps = profile["fps"]
    deltas = [
        b.received_at - a.received_at
        for a, b in zip(frames, frames[1:], strict=False)
        if 0 < b.received_at - a.received_at <= 1
    ]
    tail = statistics.median(deltas) if deltas else 1 / fps
    # A real successor receipt across the slot cut proves the final JPEG's
    # display interval. Without that evidence retain the bounded cadence tail.
    end_at = min(
        profile.get("verified_end_at", frames[-1].received_at + tail),
        profile.get("end_at", float("inf")),
    )
    duration = max(0.001, end_at - frames[0].received_at)
    manifest = buffer / f"{uuid.uuid4()}.ffconcat"
    await storage_io(destination.parent.mkdir, parents=True, exist_ok=True)
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
    width, height = profile["width"], profile["height"]
    filters = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    process = None
    try:
        await storage_io(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
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
            str(fps),
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

        if guard is not None:
            await storage_io(guard)
        await storage_io(prepare)
        await publish_generated_info(metadata, temporary, destination)
        return metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()

        def cleanup():
            if guard is not None:
                try:
                    guard()
                except OSError:
                    return  # Never clean up a fallback filesystem after mount loss.
            manifest.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

        await storage_io(cleanup)


async def merge_daily_mp4(ffmpeg: str, segments: list[dict], temporary: Path, final: Path) -> dict:
    """Stream-copy daily media with its index at EOF, avoiding a full-file relocation pass.

    Local playback can seek to the index, and downloads support HTTP byte ranges.
    Event clips retain faststart independently for their smaller standalone files.
    """
    if not segments or len({json.dumps(s["profile"], sort_keys=True) for s in segments}) != 1:
        raise RuntimeError("EXPORT_INCOMPATIBLE_MEDIA")
    final.parent.mkdir(parents=True, exist_ok=True)
    manifest = final.with_suffix(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for segment in segments:
        path = Path(segment["path"]).as_posix()
        lines.extend((f"file '../{path}'", f"duration {segment['duration']:.6f}"))
    process = None
    try:
        await storage_io(manifest.write_text, "\n".join(lines) + "\n", encoding="utf-8")
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
            *file_input_options(),
            "-i",
            str(manifest),
            "-an",
            "-c:v",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "mp4",
            str(temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(
            process.communicate(), timeout=max(3600, sum(s["duration"] for s in segments) * 2)
        )
        if process.returncode:
            raise RuntimeError(error[-1500:].decode(errors="replace"))
        metadata = await probe_media(ffmpeg, temporary, full_decode=False)
        if abs(metadata["duration"] - sum(s["duration"] for s in segments)) > max(
            0.1, len(segments) / 90000
        ):
            raise RuntimeError("Export duration differs from saved media")
        await publish_generated_info(metadata, temporary, final)
        return metadata
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        manifest.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


async def verify_archive(ffmpeg: str, path: Path, segments: list[dict]) -> dict:
    """Decode every frame once before discarding TS; never encode another video."""
    initial = file_identity(await asyncio.to_thread(path.stat))
    if not segments or len({json.dumps(s["profile"], sort_keys=True) for s in segments}) != 1:
        raise RuntimeError("EXPORT_INCOMPATIBLE_MEDIA")
    process = await start_media_process(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-xerror",
        "-threads",
        "1",
        *file_input_options(),
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-vf",
        MEDIA_INFO_FILTER,
        "-fps_mode",
        "passthrough",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        progress, error = await asyncio.wait_for(
            process.communicate(), timeout=max(3600, sum(s["duration"] for s in segments) * 2)
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    text = error.decode(errors="replace")
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    frames = re.findall(rb"frame=(\d+)", progress)
    if process.returncode or not match or not frames or "Video: h264" not in text:
        raise RuntimeError("Archive verification failed: " + text[-1500:])
    duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
    expected = sum(s["duration"] for s in segments)
    packet_count = int(frames[-1])
    expected_count = sum(round(s["duration"] * s["profile"]["fps"]) for s in segments)
    if abs(duration - expected) > max(0.1, len(segments) / 90000) or packet_count != expected_count:
        raise RuntimeError("Archive packet count or duration differs from sources")

    def durable():
        with path.open("r+b") as file:
            os.fsync(file.fileno())
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    await asyncio.to_thread(durable)
    final = file_identity(await asyncio.to_thread(path.stat))
    if initial != final:
        raise RuntimeError("Media changed during verification")
    size_bytes = final[2]
    return {
        "duration": duration,
        "size_bytes": size_bytes,
        "packet_count": packet_count,
        "media_info": parse_media_info(text, size_bytes),
        "_file_identity": final,
    }
