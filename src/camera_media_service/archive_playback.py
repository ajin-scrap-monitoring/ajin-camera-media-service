"""Bounded, ephemeral HLS segment remuxes of the canonical daily MP4."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path

from camera_media_service.catalog_pins import finish_cleanup, pin_day_locked, release_day_locked
from camera_media_service.media_files import open_media_file, storage_io
from camera_media_service.recording import ApiError, epoch


@dataclass
class _Job:
    owner: str
    reserve: int
    task: asyncio.Task | None = None
    waiters: int = 0


class ArchivedPlaybackService:
    def __init__(self, daily, *, max_bytes=64 * 1024**2, ttl_seconds=60, max_jobs=8, concurrency=2):
        if min(max_bytes, ttl_seconds, max_jobs, concurrency) <= 0:
            raise ValueError("Playback limits must be positive")
        self.daily = daily
        guard = getattr(daily.recording, "storage_guard", None)
        if guard is not None:
            guard()
        self.root = daily.recording.root / "daily-playback"
        self.root.mkdir(exist_ok=True)
        if not self.root.resolve().is_relative_to(daily.recording.root.resolve()):
            raise self._storage_error()
        self.max_bytes, self.ttl_seconds, self.max_jobs = max_bytes, ttl_seconds, max_jobs
        self.slot = asyncio.Semaphore(concurrency)
        self.jobs = {}
        self.entries = {}
        self.closed = False
        self._prune_orphans()
        with daily.db:
            daily.db.execute("DELETE FROM clip_recording_pins WHERE clip_id LIKE 'hls:%'")

    @staticmethod
    def _uuid(value):
        try:
            return str(uuid.UUID(value)) == value
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _storage_error():
        return ApiError(503, "RECORDING_STORAGE_ERROR", "Saved media unavailable.")

    def _unlink(self, path):
        try:
            guard = getattr(self.daily.recording, "storage_guard", None)
            if guard is not None:
                guard()
            path.unlink(missing_ok=True)
            return True
        except OSError:
            # In particular, Windows does not unlink a response's open handle.
            return False

    async def _snapshot(self, recording_id, segment_id):
        return await storage_io(self._read_snapshot, recording_id, segment_id)

    def _read_snapshot(self, recording_id, segment_id):
        day = self.daily.catalog.get_day(recording_id)
        if day["status"] in ("deleted", "expired"):
            raise ApiError(
                410, "DAILY_RECORDING_" + day["status"].upper(), "Recording is no longer available."
            )
        if day.get("expires_at") and epoch(day["expires_at"]) <= self.daily.clock():
            raise ApiError(410, "DAILY_RECORDING_EXPIRED", "Recording is no longer available.")
        row = self.daily.db.execute(
            "SELECT body FROM daily_segments WHERE id=? AND day_id=? AND status='ready'",
            (segment_id, recording_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "SEGMENT_NOT_FOUND", "Completed segment does not exist.")
        item = json.loads(row[0])
        if (
            day["status"] != "closed"
            or day.get("archive_state") != "mp4"
            or day["export_state"] != "ready"
        ):
            raise ApiError(409, "PLAYBACK_NOT_READY", "Archived media is not ready.")
        return self._mapping(item)

    def _mapping(self, item):
        guard = getattr(self.daily.recording, "storage_guard", None)
        if guard is not None:
            guard()
        try:
            relative = Path(item["archive_path"])
            source = (self.daily.recording.root / relative).resolve()
            owned = (self.daily.recording.root / "daily-exports").resolve()
            offset, duration = float(item["archive_offset"]), float(item["duration"])
            if (
                relative.is_absolute()
                or not owned.is_relative_to(self.daily.recording.root.resolve())
                or not source.is_relative_to(owned)
                or source.suffix != ".mp4"
                or not source.is_file()
                or not math.isfinite(offset)
                or offset < 0
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError("Invalid archive mapping")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise self._storage_error() from exc
        return item, source, offset, duration

    def _cache_path(self, key):
        path = self.root / key[0] / (key[1] + ".ts")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise self._storage_error()
        return path

    def _prune_orphans(self):
        # Only UUID entries in this private cache belong to this service. Never
        # follow a directory link during restart cleanup.
        for folder in self.root.iterdir():
            if not self._uuid(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                key = folder.name, path.stem
                if (
                    path.suffix in (".ts", ".tmp")
                    and self._uuid(path.stem)
                    and key not in self.entries
                    and key not in self.jobs
                ):
                    self._unlink(path)

    def _disk_bytes(self):
        # Count failed Windows unlinks too, including stale startup files. This
        # iterator keeps memory bounded even when cleaning an old cache.
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def _evict(self, now, reserve=0):
        guard = getattr(self.daily.recording, "storage_guard", None)
        if guard is not None:
            guard()
        self._prune_orphans()
        for key, entry in sorted(self.entries.items(), key=lambda pair: pair[1][1]):
            total = self._disk_bytes()
            allocated = sum(job.reserve for job in self.jobs.values())
            if now - entry[1] < self.ttl_seconds and total + allocated + reserve <= self.max_bytes:
                continue
            if key not in self.jobs and self._unlink(self._cache_path(key)):
                self.entries.pop(key, None)
        return (
            self._disk_bytes() + sum(job.reserve for job in self.jobs.values()) + reserve
            <= self.max_bytes
        )

    async def open_segment(self, recording_id: str, segment_id: str):
        if not self._uuid(recording_id) or not self._uuid(segment_id):
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid recording or segment identifier.")
        key = recording_id, segment_id
        async with self.daily.lock:
            if self.closed:
                raise ApiError(503, "PLAYBACK_UNAVAILABLE", "Playback service is stopping.")
            item, source, offset, duration = await self._snapshot(*key)
            await storage_io(self._evict, self.daily.clock())
            cached = await self._cached(key)
            if cached:
                return cached
            job = self.jobs.get(key)
            if job is None:
                if len(self.jobs) >= self.max_jobs:
                    raise ApiError(503, "PLAYBACK_CAPACITY_EXCEEDED", "Playback queue is full.")
                reserve = min(self.max_bytes, max(65536, int(item["size_bytes"] * 1.2) + 4096))
                if not await storage_io(self._evict, self.daily.clock(), reserve):
                    raise self._storage_error()
                cache = await storage_io(self._cache_path, key)
                job = _Job("hls:" + str(uuid.uuid4()), reserve)
                try:
                    await pin_day_locked(self.daily.recording, job.owner, recording_id)
                except BaseException:
                    await finish_cleanup(release_day_locked(self.daily.recording, job.owner))
                    raise
                self.daily.storage_reservations[job.owner] = reserve
                self.daily.storage_reservation_outputs[job.owner] = (
                    cache,
                    cache.with_suffix(".tmp"),
                )
                self.jobs[key] = job
                job.task = asyncio.create_task(self._prepare(key, job, source, offset, duration))
            job.waiters += 1
        try:
            await asyncio.shield(job.task)
            async with self.daily.lock:
                # Retention may have changed state after preparation released its
                # source lease. Open the response handle in the same critical section.
                await self._snapshot(*key)
                cached = await self._cached(key)
                if not cached:
                    raise self._storage_error()
                return cached
        finally:
            await finish_cleanup(self._finish_waiter(key, job))

    async def _finish_waiter(self, key, job):
        async with self.daily.lock:
            job.waiters -= 1
            last = job.waiters == 0
            if last and not job.task.done():
                job.task.cancel()
        if last:
            await asyncio.gather(job.task, return_exceptions=True)
            async with self.daily.lock:
                await self._release(job)
                if self.jobs.get(key) is job:
                    self.jobs.pop(key, None)

    async def _cached(self, key):
        if key not in self.entries:
            return None
        try:
            file, size = await open_media_file(
                self.root / key[0] / (key[1] + ".ts"),
                root=self.root,
                guard=getattr(self.daily.recording, "storage_guard", None),
            )
        except OSError:
            self.entries.pop(key, None)
            return None
        self.entries[key] = size, self.daily.clock()
        return file, size

    async def _release(self, job):
        await release_day_locked(self.daily.recording, job.owner)
        self.daily.storage_reservations.pop(job.owner, None)
        self.daily.storage_reservation_outputs.pop(job.owner, None)
        job.reserve = 0

    async def _release_owned(self, job):
        async with self.daily.lock:
            await self._release(job)

    async def _prepare(self, key, job, source, offset, duration):
        temporary = self.root / key[0] / (key[1] + ".tmp")
        process = None
        try:
            await storage_io(self._cache_path, key)
            async with self.slot:
                # check_space may acquire the daily lock in future implementations.
                await self.daily.check_space()
                guard = getattr(self.daily.recording, "storage_guard", None)
                if guard is not None:
                    await storage_io(guard)
                await storage_io(temporary.parent.mkdir, parents=True, exist_ok=True)
                creation = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        self.daily.recording.ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-xerror",
                        "-ss",
                        f"{offset:.9f}",
                        "-i",
                        str(source),
                        "-t",
                        f"{duration:.9f}",
                        "-map",
                        "0:v:0",
                        "-an",
                        "-c:v",
                        "copy",
                        "-muxdelay",
                        "0",
                        "-muxpreload",
                        "0",
                        "-f",
                        "mpegts",
                        "pipe:1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                )
                try:
                    process = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    # The OS may already have spawned FFmpeg when creation is
                    # cancelled. Retrieve its handle so finally can reap it.
                    process = await creation
                    raise

                async def copy():
                    size = 0
                    file, _ = await open_media_file(
                        temporary, root=self.root, guard=guard, mode="xb"
                    )
                    try:
                        while chunk := await process.stdout.read(65536):
                            size += len(chunk)
                            if size > job.reserve:
                                raise self._storage_error()
                            if guard is not None:
                                await storage_io(guard)
                            await storage_io(file.write, chunk)
                    finally:
                        await storage_io(file.close)
                    await process.wait()
                    if process.returncode or not size:
                        raise self._storage_error()
                    return size

                size = await asyncio.wait_for(copy(), timeout=120)
                async with self.daily.lock:
                    await self._snapshot(*key)
                    destination = await storage_io(self._cache_path, key)
                    await storage_io(temporary.replace, destination)
                    self.entries[key] = size, self.daily.clock()
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise self._storage_error() from exc
        finally:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # A full stdout pipe can keep Process.wait blocked even after
                # killing FFmpeg. Drain it in bounded chunks before reaping.
                while await process.stdout.read(65536):
                    pass
                await process.wait()
            await storage_io(self._unlink, temporary)
            await finish_cleanup(self._release_owned(job))

    async def maintenance(self, now: float):
        async with self.daily.lock:
            await storage_io(self._evict, now)

    async def close(self):
        async with self.daily.lock:
            self.closed = True
            jobs = list(self.jobs.values())
            for job in jobs:
                job.task.cancel()
        await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
        async with self.daily.lock:
            for job in jobs:
                await finish_cleanup(self._release(job))
            self.jobs.clear()
            await storage_io(self._evict, float("inf"))
