"""Optional NVENC with verified-output CPU fallback and owned child lifetimes."""

import asyncio
import logging

from camera_media_service.media_files import _settle_worker

LOG = logging.getLogger(__name__)


def nvenc_command(command):
    args = list(command)
    args[args.index("libx264")] = "h264_nvenc"
    if "-preset" in args:
        args[args.index("-preset") + 1] = "p5"
    if "-crf" in args:
        index = args.index("-crf")
        # CQ and x264 CRF are different scales; keep the CPU policy unchanged.
        args[index : index + 2] = ["-cq", "30"]
    index = args.index("h264_nvenc") + 1
    args[index:index] = ["-rc", "vbr", "-b:v", "0", "-profile:v", "high"]
    return args


async def _attempt(command, timeout, start):
    process = await start(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    communicating = asyncio.create_task(process.communicate())
    try:
        _, error = await asyncio.wait_for(asyncio.shield(communicating), timeout)
        if process.returncode:
            raise RuntimeError("FFmpeg encoding failed: " + error[-1500:].decode(errors="replace"))
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await _settle_worker(communicating)
        raise


class H264Encoder:
    def __init__(self, ffmpeg, mode="cpu", *, diagnostics=None):
        if mode not in ("cpu", "auto"):
            raise ValueError("MEDIA_VIDEO_ENCODER must be cpu or auto")
        self.ffmpeg = ffmpeg
        self.backend = "libx264" if mode == "cpu" else None
        self.fallbacks = 0
        self.diagnostics = diagnostics
        self.lock = asyncio.Lock()

    def _selected(self, backend):
        self.backend = backend
        if self.diagnostics is not None:
            self.diagnostics.gauge("video_encoder_nvenc", int(backend == "h264_nvenc"))

    async def _select(self, start):
        async with self.lock:
            if self.backend is not None:
                return self.backend
            probe = [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1920x1080:r=15",
                "-frames:v",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-f",
                "null",
                "-",
            ]
            try:
                await _attempt(nvenc_command(probe), 8, start)
            except (RuntimeError, TimeoutError, OSError):
                self._selected("libx264")
                LOG.info("NVENC unavailable; using CPU H.264 encoding")
            else:
                self._selected("h264_nvenc")
                LOG.info("NVENC available; using GPU H.264 encoding")
            return self.backend

    async def encode(self, command, verify, *, timeout, start):
        backend = await self._select(start)
        try:
            await _attempt(
                nvenc_command(command) if backend == "h264_nvenc" else command, timeout, start
            )
            return await verify()
        except (RuntimeError, TimeoutError):
            if backend != "h264_nvenc":
                raise
            self._selected("libx264")
            self.fallbacks += 1
            if self.diagnostics is not None:
                self.diagnostics.count("video_encoder_fallback")
            LOG.warning("NVENC encode/verification failed; retrying retained input with CPU")
            await _attempt(command, timeout, start)
            return await verify()
