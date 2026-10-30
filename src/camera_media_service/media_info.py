"""Measure video properties with FFmpeg, without requiring a separate ffprobe binary."""

from __future__ import annotations

import asyncio
import re
from fractions import Fraction
from pathlib import Path

PROBE_TIMEOUT_SECONDS = 15
# The main branch still decodes every frame for validation. Only one frame reaches
# showinfo, so a day archive cannot produce a day of per-frame diagnostic output.
MEDIA_INFO_FILTER = (
    "split[video][info];[info]trim=end_frame=1,showinfo=checksum=0,nullsink;[video]null"
)


def unknown_media_info(reason_code: str, size_bytes: int | None = None) -> dict:
    return {
        "status": "unknown",
        "source": "file_probe",
        "codec": None,
        "width": None,
        "height": None,
        "fps": None,
        "fps_num": None,
        "fps_den": None,
        "size_bytes": size_bytes,
        "reason_code": reason_code,
    }


def parse_media_info(text: str, size_bytes: int) -> dict:
    """Read decoded dimensions and the input stream's exact reported frame rate.

    This is the file's nominal stream rate, not a capture clock or an average
    inferred from configured recording settings. Rounded FFmpeg summary rates
    are deliberately insufficient when exact input information is unavailable.
    """
    input_text = text.split("Stream mapping:", 1)[0]
    codec = re.search(r"Stream #0:\d+[^\n]*?: Video: ([\w]+)\b", input_text)
    dimensions = re.search(r"\[Parsed_showinfo_[^\]]+\][^\n]*\bs:(\d+)x(\d+)\b", text)
    rate = re.search(
        r"\[Parsed_showinfo_[^\]]+\] config in [^\n]*frame_rate:\s*(\d+)/(\d+)",
        text,
    )
    if not codec or not dimensions or not rate or min(map(int, rate.groups())) <= 0:
        return unknown_media_info("metadata_unavailable", size_bytes)
    width, height = map(int, dimensions.groups())
    if width <= 0 or height <= 0:
        return unknown_media_info("metadata_unavailable", size_bytes)
    fps = Fraction(int(rate[1]), int(rate[2]))
    return {
        "status": "ready",
        "source": "file_probe",
        "codec": codec[1],
        "width": width,
        "height": height,
        "fps": float(fps),
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "size_bytes": size_bytes,
    }


async def inspect_media_info(ffmpeg: str, path: Path) -> dict:
    """Inspect at most three initial frames; failures are explicit unknown values.

    Callers own admission limits and caching. Cancellation always propagates and
    reaps the child, including cancellation while OS process creation is pending.
    This header inspection does not establish whole-file media integrity.
    """
    from camera_media_service.encoding import start_media_process

    try:
        initial = await asyncio.to_thread(path.stat)
    except OSError:
        return unknown_media_info("file_unavailable")

    async def inspect():
        process = await start_media_process(
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-xerror",
            "-threads",
            "2",
            "-probesize",
            "1048576",
            "-analyzeduration",
            "1000000",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            MEDIA_INFO_FILTER,
            "-frames:v",
            "3",
            "-nostats",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, error = await process.communicate()
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.communicate()
        if process.returncode:
            return unknown_media_info("probe_failed", initial.st_size)
        final = await asyncio.to_thread(path.stat)
        if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        ):
            return unknown_media_info("file_changed")
        return parse_media_info(error.decode(errors="replace"), final.st_size)

    try:
        return await asyncio.wait_for(inspect(), PROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        return unknown_media_info("probe_timeout", initial.st_size)
    except OSError:
        return unknown_media_info("probe_unavailable", initial.st_size)
