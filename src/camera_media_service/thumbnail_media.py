"""One bounded real decoded frame; no whole-video transcoding or placeholders."""

from __future__ import annotations

import asyncio
from pathlib import Path

from camera_media_service.encoding import start_media_process
from camera_media_service.media_files import storage_io

MAX_JPEG_BYTES = 512 * 1024


class ThumbnailFailure(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(bytes((255, 216))) or not data.endswith(bytes((255, 217))):
        raise ThumbnailFailure("INVALID_IMAGE")
    position = 2
    for _ in range(1024):
        if position + 4 > len(data) or data[position] != 255:
            break
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in (0xD8, 0xD9, 0xDA):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if length < 8:
                break
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            if width > 0 and height > 0:
                return width, height
            break
        position += length
    raise ThumbnailFailure("INVALID_IMAGE")


async def generate_thumbnail(
    ffmpeg: str, source: Path, output: Path, *, timeout: float = 15, max_bytes: int = MAX_JPEG_BYTES
) -> dict:
    process = None
    success = False
    try:
        async with asyncio.timeout(timeout):
            process = await start_media_process(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-xerror",
                "-threads",
                "1",
                "-probesize",
                "1048576",
                "-analyzeduration",
                "1000000",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                "scale=w='min(640,iw)':h='min(360,ih)':force_original_aspect_ratio=decrease,setsar=1",
                "-filter_threads",
                "1",
                "-frames:v",
                "1",
                "-c:v",
                "mjpeg",
                "-q:v",
                "3",
                "-threads",
                "1",
                "-f",
                "image2",
                "-update",
                "1",
                "-y",
                str(output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            if process.returncode:
                exists = await storage_io(output.exists)
                raise ThumbnailFailure("GENERATION_FAILED" if exists else "NO_VIDEO_FRAME")

            def inspect():
                with output.open("rb") as file:
                    data = file.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ThumbnailFailure("IMAGE_TOO_LARGE")
                width, height = jpeg_dimensions(data)
                if width > 640 or height > 360:
                    raise ThumbnailFailure("INVALID_IMAGE")
                return {"width": width, "height": height, "size_bytes": len(data)}

            try:
                result = await storage_io(inspect)
            except FileNotFoundError as exc:
                raise ThumbnailFailure("NO_VIDEO_FRAME") from exc
            success = True
            return result
    finally:

        async def cleanup():
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.communicate()
            if not success:
                await storage_io(output.unlink, missing_ok=True)

        task = asyncio.create_task(cleanup())
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        task.result()
        if interrupted:
            await storage_io(output.unlink, missing_ok=True)
            raise asyncio.CancelledError
