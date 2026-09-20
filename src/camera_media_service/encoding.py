"""Encode pinned JPEG frames; no continuous transcoding of the live stream."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


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
