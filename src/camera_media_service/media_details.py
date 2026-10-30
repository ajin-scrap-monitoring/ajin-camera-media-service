"""Bounded actual-file inspection and explicit capture-time availability."""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict

from camera_media_service.media_info import inspect_media_info, unknown_media_info
from camera_media_service.recording import ApiError


def timestamp_info():
    return {
        "time_basis": "server_received_at",
        "capture_timestamp_supported": False,
        "capture_timestamp": None,
        "capture_clock_source": None,
        "clock_synchronization": "unavailable",
    }


def thumbnail_info():
    return {"status": "unsupported", "representative_at": None, "content_path": None}


class MediaDetails:
    def __init__(self, recording, daily=None, exports=None):
        self.recording, self.daily, self.exports = recording, daily, exports
        self.slot = asyncio.Semaphore(2)
        self.active = 0
        self.cache = OrderedDict()

    async def inspect(self, path):
        try:
            stat = await asyncio.to_thread(path.stat)
        except OSError:
            return unknown_media_info("file_unavailable")
        fingerprint = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if fingerprint in self.cache:
            self.cache.move_to_end(fingerprint)
            return dict(self.cache[fingerprint])
        if self.active >= 8:
            raise ApiError(503, "METADATA_CAPACITY_EXCEEDED", "Media inspection queue is full.")
        self.active += 1
        try:
            async with self.slot:
                result = await inspect_media_info(self.recording.ffmpeg, path)
            if result["status"] == "ready":
                self.cache[fingerprint] = dict(result)
                self.cache.move_to_end(fingerprint)
                while len(self.cache) > 128:
                    self.cache.popitem(last=False)
            return result
        finally:
            self.active -= 1

    async def clip(self, clip_id):
        async with self.recording.lock:
            body = self.recording._get(clip_id)
            await self.recording._expire(body)
            self._clip_access(body)
            path = (self.recording.clips / (body["clip_id"] + ".mp4")).resolve()
            if not path.is_relative_to(self.recording.clips.resolve()):
                raise ApiError(503, "STORAGE_UNAVAILABLE", "Media path is unavailable.")
        measured = await self.inspect(path)
        async with self.recording.lock:
            body = self.recording._get(clip_id)
            await self.recording._expire(body)
            self._clip_access(body)
        return {
            "clip_id": clip_id,
            "size_bytes": measured["size_bytes"],
            "media_info": measured,
            "timestamp_info": timestamp_info(),
            "thumbnail": thumbnail_info(),
        }

    @staticmethod
    def _clip_access(body):
        if body["status"] in ("deleted", "expired"):
            raise ApiError(410, "CLIP_" + body["status"].upper(), "Clip is no longer available.")
        if body["status"] != "ready":
            raise ApiError(409, "CLIP_NOT_READY", "Clip is not ready.")

    async def day(self, recording_id):
        owner = "info:" + str(uuid.uuid4())
        async with self.recording.lock:
            day = self.daily.catalog.get_day(recording_id)
            self.exports._accessible(day)
            parts = self.daily.catalog.segments(recording_id, ready_only=True)
            master = self.exports._path(recording_id) if day["export_state"] == "ready" else None
            sample = next(
                (
                    p
                    for p in parts
                    if not p.get("media_info") or p["media_info"]["status"] != "ready"
                ),
                None,
            )
            sample_path = None
            if sample and day.get("archive_state") != "mp4":
                sample_path = (self.recording.root / sample["path"]).resolve()
                if not sample_path.is_relative_to((self.recording.root / "daily").resolve()):
                    raise ApiError(503, "STORAGE_UNAVAILABLE", "Media path is unavailable.")
            with self.recording.db:
                self.recording.db.execute(
                    "INSERT INTO clip_recording_pins VALUES(?,?)", (owner, recording_id)
                )
        try:
            file_info = await self.inspect(master) if master else None
            sample_info = await self.inspect(sample_path) if sample_path else None
            formats = {}
            measured = 0
            for part in parts:
                info = sample_info if part is sample and sample_info else part.get("media_info")
                if not info or info["status"] != "ready":
                    continue
                measured += 1
                key = tuple(info[k] for k in ("codec", "width", "height", "fps_num", "fps_den"))
                if key not in formats:
                    formats[key] = {
                        k: info[k]
                        for k in ("codec", "width", "height", "fps", "fps_num", "fps_den")
                    }
                    formats[key]["segment_count"] = 0
                formats[key]["segment_count"] += 1
            total = len(parts)
            mixed = True if len(formats) > 1 else False if measured == total and measured else None
            async with self.recording.lock:
                self.exports._accessible(self.daily.catalog.get_day(recording_id))
            return {
                "recording_id": recording_id,
                "file": file_info,
                "segments": {
                    "status": "ready"
                    if measured == total and measured
                    else "partial"
                    if measured
                    else "unknown",
                    "total": total,
                    "measured": measured,
                    "mixed": mixed,
                    "formats": list(formats.values()),
                },
                "timestamp_info": timestamp_info(),
                "thumbnail": thumbnail_info(),
            }
        finally:
            async with self.recording.lock:
                with self.recording.db:
                    self.recording.db.execute(
                        "DELETE FROM clip_recording_pins WHERE clip_id=?", (owner,)
                    )
