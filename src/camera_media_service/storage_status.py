"""Coalesced, read-only storage observations; policy limits are separate layers."""

import asyncio
import shutil
import time
from pathlib import Path

from camera_media_service.catalog_pins import finish_cleanup
from camera_media_service.media_files import storage_io
from camera_media_service.media_usage import iter_regular_files
from camera_media_service.recording import iso

CATEGORIES = ("daily", "daily-exports", "daily-playback", "daily-thumbnails")


class StorageStatus:
    def __init__(self, recording, daily=None, *, ttl_seconds=2):
        self.recording, self.daily, self.ttl_seconds = recording, daily, ttl_seconds
        self._cache = None
        self._cache_generation = None
        self._deadline = 0
        self._flight = None
        self._closed = False

    def _limits(self):
        settings = getattr(self.daily, "settings", None)
        return {
            "media_limit_bytes": getattr(
                settings, "max_media_storage_bytes", self.recording.max_storage_bytes
            ),
            "daily_limit_bytes": getattr(settings, "max_daily_storage_bytes", None),
        }

    def _unavailable(self, reason):
        return dict(
            self._limits(),
            status="unavailable",
            observed_at=None,
            observation_started_at=None,
            storage_type="filesystem",
            filesystem_total_bytes=None,
            filesystem_available_bytes=None,
            daily_usage_bytes=None,
            daily_reserved_bytes=None,
            reason_code=reason,
        )

    def _state(self):
        return {
            "generation": self.recording._budget_generation,
            "root": self.recording.root,
            "guard": self.recording.storage_guard,
            "reservations": dict(self.recording.capacity_reservations),
            "outputs": {
                owner: tuple(paths) for owner, paths in self.recording.capacity_outputs.items()
            },
            "limits": self._limits(),
        }

    def _observe(self, state):
        root, guard = state["root"], state["guard"]
        guard()
        began = iso(time.time())
        disk = shutil.disk_usage(root)
        if not 0 <= disk.free <= disk.total:
            raise OSError("Invalid filesystem capacity observation")
        owners = {}
        allocated = {}
        unknown_scope = False
        for owner, planned in state["reservations"].items():
            paths = state["outputs"].get(owner, ())
            if planned and not paths:
                unknown_scope = True
            for path in paths:
                try:
                    relative = Path(path).relative_to(root)
                except ValueError:
                    continue
                if relative.parts and relative.parts[0] in CATEGORIES:
                    owners[str(path)] = owner
                    allocated[owner] = 0
        used = 0
        for category in CATEGORIES:
            folder = root / category
            if folder.is_symlink():
                raise OSError("Invalid managed storage directory")
            for path, info, _ in iter_regular_files(folder, category=category):
                used += info.st_size
                owner = owners.get(path)
                if owner in allocated:
                    allocated[owner] += info.st_size
        reserved = (
            None
            if unknown_scope
            else sum(
                max(0, state["reservations"][owner] - size) for owner, size in allocated.items()
            )
        )
        guard()
        return dict(
            state["limits"],
            status="ready",
            storage_type="filesystem",
            observation_started_at=began,
            observed_at=iso(time.time()),
            filesystem_total_bytes=int(disk.total),
            filesystem_available_bytes=int(disk.free),
            daily_usage_bytes=used,
            daily_reserved_bytes=reserved,
            reason_code="RESERVATION_SCOPE_UNKNOWN" if unknown_scope else None,
        )

    async def _compute(self):
        try:
            for _ in range(2):
                async with self.recording.lock:
                    state = self._state()
                result = await storage_io(self._observe, state)
                async with self.recording.lock:
                    if state["generation"] != self.recording._budget_generation:
                        continue
                    self._cache = result
                    self._cache_generation = state["generation"]
                    self._deadline = time.monotonic() + self.ttl_seconds
                    return dict(result)
            return self._unavailable("STORAGE_SNAPSHOT_BUSY")
        except OSError:
            return self._unavailable("STORAGE_UNAVAILABLE")

    async def snapshot(self):
        if self._closed:
            return self._unavailable("STORAGE_UNAVAILABLE")
        try:
            await storage_io(self.recording.storage_guard)
        except OSError:
            self._cache = None
            return self._unavailable("STORAGE_UNAVAILABLE")
        if self._closed:
            return self._unavailable("STORAGE_UNAVAILABLE")
        if (
            self._cache is not None
            and time.monotonic() < self._deadline
            and self._cache_generation == self.recording._budget_generation
        ):
            return dict(self._cache)
        if self._flight is None or self._flight.done():
            self._flight = asyncio.create_task(self._compute())
        return dict(await asyncio.shield(self._flight))

    async def close(self):
        self._closed = True
        if self._flight is not None:
            self._flight.cancel()

            async def drain():
                await asyncio.gather(self._flight, return_exceptions=True)

            await finish_cleanup(drain())
