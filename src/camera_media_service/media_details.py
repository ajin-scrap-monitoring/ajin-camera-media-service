"""Bounded actual-file inspection and explicit capture-time availability."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections import OrderedDict

from camera_media_service.catalog_pins import finish_cleanup, pinned_snapshot
from camera_media_service.media_files import storage_io
from camera_media_service.media_info import inspect_media_info, response_info, unknown_media_info
from camera_media_service.metadata_reads import read_measurement, read_segment_measurements
from camera_media_service.persisted_media_info import (
    file_identity,
    persist_generated_info,
)
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
        self.flights = {}
        self.day_flights = {}
        self.day_slot = asyncio.Semaphore(2)
        self.closed = False
        self.db = getattr(recording, "db", None)

    def _available(self):
        if self.closed:
            raise ApiError(503, "METADATA_UNAVAILABLE", "Media inspection is stopped.")

    async def close(self):
        self.closed = True

        async def drain():
            tasks = [
                flight["task"] for flight in (*self.day_flights.values(), *self.flights.values())
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        await finish_cleanup(drain())

    async def _identity(self, path):
        return file_identity(await asyncio.to_thread(path.stat))

    def _remember(self, fingerprint, result):
        self.cache[fingerprint] = dict(result)
        self.cache.move_to_end(fingerprint)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)

    async def inspect(self, path):
        self._available()
        try:
            identity = await self._identity(path)
        except OSError:
            return unknown_media_info("file_unavailable")
        fingerprint = (str(path), *identity)
        result = self.cache.get(fingerprint)
        origin = "memory_cache" if result is not None else "persisted_probe"
        persisted = (
            await storage_io(
                read_measurement,
                self.recording.root,
                path,
                identity,
                getattr(self.recording, "storage_guard", None),
            )
            if self.db
            else None
        )
        if result is None:
            result = persisted
        if result is not None:
            try:
                if await self._identity(path) != identity:
                    return unknown_media_info("file_changed")
            except OSError:
                return unknown_media_info("file_unavailable")
            if self.db is not None and persisted is None:
                await self._persist(path, identity, result)
                try:
                    if await self._identity(path) != identity:
                        return unknown_media_info("file_changed")
                except OSError:
                    return unknown_media_info("file_unavailable")
            self._remember(fingerprint, result)
            return response_info(result, origin)
        flight = self.flights.get(fingerprint)
        if flight is None:
            self._available()
            if self.active >= 8:
                raise ApiError(503, "METADATA_CAPACITY_EXCEEDED", "Media inspection queue is full.")
            self.active += 1
            flight = {
                "task": asyncio.create_task(self._probe(path, identity, fingerprint)),
                "waiters": 0,
            }
            self.flights[fingerprint] = flight
        flight["waiters"] += 1
        try:
            return response_info(await asyncio.shield(flight["task"]), "request_probe")
        finally:
            flight["waiters"] -= 1
            if not flight["waiters"]:
                if not flight["task"].done():
                    flight["task"].cancel()
                    cleanup = asyncio.gather(flight["task"], return_exceptions=True)
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # Repeated shutdown cancellation cannot detach the
                            # probe while its child process is being reaped.
                            continue
                if self.flights.get(fingerprint) is flight:
                    self.flights.pop(fingerprint, None)

    async def _probe(self, path, identity, fingerprint):
        try:
            async with self.slot:
                # Queueing can outlive the original file; never probe its replacement
                # under the previous identity.
                try:
                    if await self._identity(path) != identity:
                        return unknown_media_info("file_changed")
                except OSError:
                    return unknown_media_info("file_unavailable")
                result = await inspect_media_info(self.recording.ffmpeg, path)
            try:
                if await self._identity(path) != identity:
                    return unknown_media_info("file_changed")
            except OSError:
                return unknown_media_info("file_unavailable")
            if result["status"] == "ready":
                if self.db is not None:
                    await self._persist(path, identity, result)
                    try:
                        if await self._identity(path) != identity:
                            return unknown_media_info("file_changed")
                    except OSError:
                        return unknown_media_info("file_unavailable")
                self._remember(fingerprint, result)
            return result
        finally:
            self.active -= 1

    async def _persist(self, path, identity, result):
        try:
            if await self._identity(path) != identity:
                return
            await persist_generated_info(
                self.recording, path, {"media_info": result, "_file_identity": identity}
            )
        except (sqlite3.Error, OSError):
            # An identity-bound optional cache can be retried after failure.
            pass

    async def clip(self, clip_id):
        async with self.recording.lock:
            body = await storage_io(self.recording._get, clip_id)
            await self.recording._expire(body)
            self._clip_access(body)

            def owned_path():
                path = (self.recording.clips / (body["clip_id"] + ".mp4")).resolve()
                if not path.is_relative_to(self.recording.clips.resolve()):
                    raise ApiError(503, "STORAGE_UNAVAILABLE", "Media path is unavailable.")
                return path

            path = await storage_io(owned_path)
        measured = await self.inspect(path)
        async with self.recording.lock:
            body = await storage_io(self.recording._get, clip_id)
            await self.recording._expire(body)
            self._clip_access(body)
        return {
            "clip_id": clip_id,
            "size_bytes": measured["size_bytes"],
            "media_info": measured,
            "timestamp_info": timestamp_info(),
            "thumbnail": thumbnail_info(),
        }

    def _clip_access(self, body):
        self.recording._camera(body["camera_id"])
        if body["status"] in ("deleted", "expired"):
            raise ApiError(410, "CLIP_" + body["status"].upper(), "Clip is no longer available.")
        if body["status"] != "ready":
            raise ApiError(409, "CLIP_NOT_READY", "Clip is not ready.")

    async def _stored_segment_info(self, path):
        try:
            identity = await self._identity(path)
            result = await storage_io(
                read_measurement,
                self.recording.root,
                path,
                identity,
                getattr(self.recording, "storage_guard", None),
            )
            if result is not None and await self._identity(path) == identity:
                return result
        except OSError:
            pass
        return None

    async def day(self, recording_id):
        self._available()
        flight = self.day_flights.get(recording_id)
        if flight is None:
            if len(self.day_flights) >= 8:
                raise ApiError(503, "METADATA_CAPACITY_EXCEEDED", "Media inspection queue is full.")
            flight = {"task": asyncio.create_task(self._bounded_day(recording_id)), "waiters": 0}
            self.day_flights[recording_id] = flight
        flight["waiters"] += 1
        try:
            result = await asyncio.shield(flight["task"])
            async with self.recording.lock:
                self.exports._accessible(await storage_io(self.daily.catalog.get_day, recording_id))
            return result
        finally:
            flight["waiters"] -= 1
            if not flight["waiters"]:

                async def release():
                    if not flight["task"].done():
                        flight["task"].cancel()
                    await asyncio.gather(flight["task"], return_exceptions=True)
                    if self.day_flights.get(recording_id) is flight:
                        self.day_flights.pop(recording_id, None)

                await finish_cleanup(release())

    async def _bounded_day(self, recording_id):
        async with self.day_slot:
            self._available()
            return await self._day(recording_id)

    async def _day(self, recording_id):
        owner = "info:" + str(uuid.uuid4())

        def snapshot():
            day = self.daily.catalog.get_day(recording_id)
            self.recording._camera(day["camera_id"])
            self.exports._accessible(day)
            parts = self.daily.catalog.segments(recording_id, ready_only=True)
            master = self.exports._path(recording_id) if day["export_state"] == "ready" else None
            return day, parts, master

        async with pinned_snapshot(self.recording, owner, recording_id, snapshot) as snapshot_data:
            day, parts, master = snapshot_data
            file_info = await self.inspect(master) if master else None
            sample, sample_info = None, None
            if day.get("archive_state") != "mp4":
                # A day-info demand validates only that day's files. Startup and
                # day listing never scan historical media or start probe work.
                measurements = await storage_io(
                    read_segment_measurements,
                    self.recording.root,
                    parts,
                    getattr(self.recording, "storage_guard", None),
                )
                for part, (path, _identity, info) in zip(parts, measurements, strict=True):
                    part["media_info"] = info
                    if sample is None and part["media_info"] is None:
                        sample, sample_path = part, path
                if sample is not None:
                    sample_info = await self.inspect(sample_path)
            else:
                association = None
                if master:
                    try:
                        identity = await self._identity(master)
                        association = await storage_io(
                            read_measurement,
                            self.recording.root,
                            master,
                            identity,
                            getattr(self.recording, "storage_guard", None),
                            archive=True,
                        )
                        if await self._identity(master) != identity:
                            association = None
                    except OSError:
                        pass
                # A header re-probe cannot attest historical per-segment formats.
                # Only a full archive generation/verification can establish this link.
                for part in parts:
                    part["media_info"] = (association or {}).get(part["segment_id"])
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
                self.exports._accessible(await storage_io(self.daily.catalog.get_day, recording_id))
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
