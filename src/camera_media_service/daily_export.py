"""Automatic canonical daily MP4 archives, retention and safe source compaction."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from camera_media_service.archive_playback import ArchivedPlaybackService
from camera_media_service.background_media import BackgroundMedia
from camera_media_service.catalog_pins import finish_cleanup
from camera_media_service.daily_recording import DailyRecordingService
from camera_media_service.encoding import merge_daily_mp4, verify_archive
from camera_media_service.media_files import storage_io
from camera_media_service.persisted_media_info import (
    load_verified_archive,
    persist_generated_info,
    source_measurements,
)
from camera_media_service.recording import ApiError, epoch, iso

LOG = logging.getLogger(__name__)


class DailyExportService:
    def __init__(self, daily: DailyRecordingService, settings):
        self.daily, self.settings = daily, settings
        self.db, self.lock = daily.db, daily.lock
        self.root = daily.recording.root
        self.output = self.root / "daily-exports"
        self.output.mkdir(exist_ok=True)
        self.jobs = {}
        self.slot = asyncio.Semaphore(1)
        self.maintenance_lock = asyncio.Lock()
        self.file_locks = {}
        self.task = None
        self.closed = False
        self.compaction_owners = set()
        self.background = BackgroundMedia(
            daily.recording,
            clock=daily.clock,
            # A capacity-frozen cursor must not stop the existing archive that
            # must finish before duplicate bytes can be reclaimed. Actual input
            # queue/byte pressure and storage errors still pause its work.
            backlog_exemption=lambda: bool(daily.capacity_pending and self.jobs),
        )
        self.reservations = daily.storage_reservations
        self.reservation_outputs = daily.storage_reservation_outputs
        self.playback = ArchivedPlaybackService(daily)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_cleanup(day_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS daily_compactions(day_id TEXT PRIMARY KEY);
        """)
        with self.db:
            # Recover unfinished deletion even if interrupted before queue insertion.
            for row in self.db.execute(
                "SELECT body FROM daily_recordings WHERE status IN ('deleted','expired')"
            ).fetchall():
                day = daily.catalog.decode_day(row["body"])
                if day.get("cleanup_state") != "complete":
                    day.setdefault("cleanup_state", "pending")
                    day.update(cleanup_completed_at=None, space_reclaimed=None)
                    day.setdefault("cleanup_error", None)
                    daily.catalog.save_day(day)
                    self.db.execute(
                        "INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (day["recording_id"],)
                    )
            for row in self.db.execute(
                "SELECT day_id,body FROM daily_exports WHERE status='processing'"
            ).fetchall():
                job = json.loads(row["body"])
                job.update(
                    status="failed",
                    error={"code": "JOB_INTERRUPTED", "message": "Archive interrupted by restart."},
                )
                self.db.execute(
                    "UPDATE daily_exports SET status=?,body=? WHERE day_id=?",
                    ("failed", json.dumps(job), row["day_id"]),
                )
                day = daily.catalog.get_day(row["day_id"])
                day.update(export_state="failed", error=job["error"], archive_retry_at=None)
                daily.catalog.save_day(day)
                (self.output / (row["day_id"] + ".tmp.mp4")).unlink(missing_ok=True)
                (self.output / (row["day_id"] + ".ffconcat")).unlink(missing_ok=True)

        for row in self.db.execute("SELECT day_id FROM daily_compactions"):
            day = self.daily.catalog.get_state(row[0])
            if day.get("archive_state") == "mp4":
                self.compaction_owners.add(row[0])
                self.reservations[row[0]] = 0
                self.reservation_outputs[row[0]] = (self._path(row[0]),)

    def _release_workspace(self, key):
        self.compaction_owners.discard(key)
        self.reservations.pop(key, None)
        self.reservation_outputs.pop(key, None)

    async def _commit_workspace_release(self, key, *, day=None, cleanup=False):
        # The caller holds the catalogue lock until both the durable transition
        # and its in-memory owner release finish, even after repeated cancellation.
        async def commit():
            async with self.daily.recording.transaction():
                if day is not None:
                    self.daily.catalog.save_day(day)
                self.db.execute("DELETE FROM daily_compactions WHERE day_id=?", (key,))
                if cleanup:
                    self.db.execute("DELETE FROM daily_cleanup WHERE day_id=?", (key,))
            self._release_workspace(key)

        await finish_cleanup(commit())

    def _accessible(self, day: dict) -> None:
        expired = day["expires_at"] and epoch(day["expires_at"]) <= self.daily.clock()
        if day["status"] in ("deleted", "expired") or expired:
            state = "deleted" if day["status"] == "deleted" else "expired"
            raise ApiError(
                410, "DAILY_RECORDING_" + state.upper(), "Recording is no longer available."
            )

    def _exportable(self, day: dict) -> None:
        self._accessible(day)
        if self.daily.clock() < epoch(day["window_end_at"]):
            raise ApiError(409, "DAY_NOT_CLOSED", "Today cannot be archived.")
        if day["status"] == "finalizing":
            raise ApiError(409, "DAY_FINALIZING", "Day is being finalized.")
        if day["status"] != "closed":
            raise ApiError(409, "DAY_NOT_CLOSED", "Day is not closed.")
        if day["recorded_seconds"] <= 0:
            raise ApiError(409, "NO_RECORDING_DATA", "No saved media.")

    async def request_export(self, recording_id: str) -> tuple[dict, bool]:
        """Compatibility status check: never start whole-day file work from a click."""
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            self._exportable(day)
            if day.get("archive_state") == "mp4" and not self._path(recording_id).is_file():
                raise ApiError(503, "RECORDING_STORAGE_ERROR", "Archived MP4 is unavailable.")
            return day, False

    def _path(self, recording_id: str):
        uuid.UUID(recording_id)
        return self.output / (recording_id + ".mp4")

    async def _enqueue(self, recording_id: str) -> None:
        if self.background.pressured():
            self.daily.recording.diagnostics.count("archive_admission_deferred")
            return
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            self._exportable(day)
            if day.get("archive_state") == "mp4" or self._has_job(recording_id):
                return
            # One archive at a time also bounds temporary duplicate disk reservations.
            if self.jobs or self.compaction_owners:
                return
            segments = self.daily.catalog.segments(recording_id, ready_only=True)
            reuse = self._path(recording_id).is_file()
            reserve = 0 if reuse else int(sum(s["size_bytes"] for s in segments) * 1.2) + 1048576
        if reserve:
            await self.daily.check_space(reserve)
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            self._exportable(day)
            if (
                self.closed
                or self.jobs
                or self.compaction_owners
                or day.get("archive_state") == "mp4"
            ):
                return
            job = {
                "job_id": str(uuid.uuid4()),
                "recording_id": recording_id,
                "revision": day["revision"],
                "status": "processing",
                "error": None,
            }
            async with self.daily.recording.transaction():
                self.db.execute(
                    "INSERT OR REPLACE INTO daily_exports VALUES(?,?,?)",
                    (recording_id, "processing", json.dumps(job)),
                )
                day.update(
                    export_state="processing",
                    download_path=None,
                    size_bytes=None,
                    export_expires_at=None,
                    archive_retry_at=None,
                    error=None,
                )
                self.daily.catalog.save_day(day)
            self.reservations[recording_id] = reserve
            self.reservation_outputs[recording_id] = (
                self.output / (recording_id + ".tmp.mp4"),
                self._path(recording_id),
            )
            task = asyncio.create_task(self._export(job, segments, reuse=reuse))
            self.jobs[recording_id] = task
            task.add_done_callback(lambda done, key=recording_id: self._done(key, done))

    def _done(self, key, task):
        if self.jobs.get(key) is task:
            self.jobs.pop(key, None)
            if key not in self.compaction_owners:
                self._release_workspace(key)
        if not task.cancelled() and task.exception():
            LOG.error("Daily archive failed", exc_info=task.exception())

    def _has_job(self, key):
        task = self.jobs.get(key)
        if task is None:
            return False
        if task.done():
            self._done(key, task)
            return False
        return True

    async def _export(self, job: dict, segments: list[dict], *, reuse: bool = False) -> None:
        async with self.background.scope():
            await self._export_owned(job, segments, reuse=reuse)

    async def _export_owned(self, job: dict, segments: list[dict], *, reuse: bool = False) -> None:
        recording_id = job["recording_id"]
        final = self._path(recording_id)
        error, metadata = None, None
        association = {}
        try:
            async with self.slot:
                sources_before = (
                    await source_measurements(self.daily.recording, segments) if not reuse else {}
                )
                if not reuse:
                    await self.daily.check_space()
                    with self.daily.recording.diagnostics.measure("archive_merge"):
                        await merge_daily_mp4(
                            self.daily.recording.ffmpeg,
                            segments,
                            self.output / (recording_id + ".tmp.mp4"),
                            final,
                        )
                # Verification reads the finished archive and allocates no more
                # video output. Its existing bytes remain in disk usage.
                self.reservations[recording_id] = 0
                await self.daily.check_space()
                with self.daily.recording.diagnostics.measure("archive_verify"):
                    metadata = await verify_archive(self.daily.recording.ffmpeg, final, segments)
                if reuse:
                    identity = metadata.get("_file_identity")
                    association = (
                        load_verified_archive(self.db, self.root, final, identity) or {}
                        if identity
                        else {}
                    )
                else:
                    sources_after = await source_measurements(self.daily.recording, segments)
                    association = {
                        key: item["media_info"]
                        for key, item in sources_before.items()
                        if sources_after.get(key) == item
                    }
        except asyncio.CancelledError:
            error = {"code": "JOB_INTERRUPTED", "message": "Archive interrupted."}
        except (OSError, RuntimeError, ApiError, TimeoutError) as exc:
            code = (
                exc.code
                if isinstance(exc, ApiError)
                else "STORAGE_ERROR"
                if isinstance(exc, OSError)
                else "EXPORT_INCOMPATIBLE_MEDIA"
                if "EXPORT_INCOMPATIBLE_MEDIA" in str(exc)
                else "EXPORT_FAILED"
            )
            error = {"code": code, "message": "Automatic daily MP4 preparation failed."}
            LOG.exception("Daily archive failed: %s", recording_id)
        if not error and metadata:
            try:
                await persist_generated_info(
                    self.daily.recording, final, metadata, archive_segments=association
                )
                with self.daily.recording.diagnostics.measure("archive_mapping"):
                    await self._stage_archive_mappings(job, segments, final)
            except asyncio.CancelledError:
                error = {"code": "JOB_INTERRUPTED", "message": "Archive interrupted."}
            except (sqlite3.Error, OSError, ApiError):
                LOG.exception("Archive mapping preparation failed: %s", recording_id)
                error = {"code": "STORAGE_ERROR", "message": "Archive mapping persistence failed."}

        async def publish():
            async with self.lock:
                async with self.daily.recording.transaction():
                    failure = await storage_io(
                        self._publish_archive, job, segments, final, metadata, error
                    )
                if not failure:
                    self.compaction_owners.add(recording_id)
                    self.daily.recording.notify_thumbnail_ready("day", recording_id, metadata)
            if failure:
                await storage_io(final.unlink, missing_ok=True)

        await finish_cleanup(publish())

    def _publish_archive(self, job, segments, final, metadata, error):
        recording_id = job["recording_id"]
        day = self.daily.catalog.get_day(recording_id)
        if day["status"] in ("expired", "deleted") or (
            day["expires_at"] and epoch(day["expires_at"]) <= self.daily.clock()
        ):
            error = {"code": "RECORDING_EXPIRED", "message": "Day expired during archive."}
        job.update(status="failed" if error else "ready", error=error)
        self.db.execute(
            "UPDATE daily_exports SET status=?,body=? WHERE day_id=?",
            (job["status"], json.dumps(job), recording_id),
        )
        if error:
            day.update(
                export_state="failed",
                download_path=None,
                size_bytes=None,
                export_expires_at=None,
                archive_retry_at=iso(self.daily.clock() + 30),
                error=error,
            )
        else:
            day.update(
                archive_state="mp4",
                export_state="ready",
                size_bytes=metadata["size_bytes"],
                media_info=metadata.get("media_info"),
                download_path=f"/api/v2/daily-recordings/{recording_id}/content",
                export_expires_at=day["expires_at"],
                archive_retry_at=None,
                error=None,
            )
            self.db.execute("INSERT OR IGNORE INTO daily_compactions VALUES(?)", (recording_id,))
        self.daily.catalog.save_day(day, notify=False)
        return error

    async def _stage_archive_mappings(self, job, segments, final):
        # Readers keep using TS until the final small ready transaction. Earlier
        # batches are safe to repeat after restart; TS compaction is not admitted
        # until all offsets are durable and the verified master is published.
        offset_us = 0
        for start in range(0, len(segments), 64):
            await self.background.checkpoint()
            async with self.lock:
                self._accessible(self.daily.catalog.get_state(job["recording_id"]))
                async with self.daily.recording.transaction():
                    offset_us = await storage_io(
                        self._stage_mapping_batch, segments[start : start + 64], final, offset_us
                    )

    def _stage_mapping_batch(self, batch, final, offset_us):
        for segment in batch:
            segment.update(
                archive_path=str(final.relative_to(self.root)), archive_offset=offset_us / 1_000_000
            )
            self.db.execute(
                "UPDATE daily_segments SET body=? WHERE id=?",
                (json.dumps(segment), segment["segment_id"]),
            )
            offset_us += round(segment["duration"] * 1_000_000)
        return offset_us

    def _file_lock(self, recording_id: str) -> asyncio.Lock:
        return self.file_locks.setdefault(recording_id, asyncio.Lock())

    async def _file_work(self, operation) -> None:
        # Cancelling to_thread does not stop its OS thread. Keep the day lock until
        # that thread finishes, including repeated cancellation during shutdown.
        worker = asyncio.create_task(asyncio.to_thread(operation))
        cancelled = False
        while True:
            try:
                await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _remove_folder(folder: Path) -> None:
        try:
            shutil.rmtree(folder)
        except FileNotFoundError as exc:
            # Python 3.13 already ignores nested ENOENT. Only an absent owned
            # root is success here; other storage failures must remain visible.
            if exc.filename is None or Path(exc.filename) != folder:
                raise

    def _compaction_pressured(self):
        queue = self.daily.recording.queue
        if queue is not None and (
            queue.qsize() > 60 or getattr(queue, "queued_bytes", 0) > 8 * 1024**2
        ):
            return True
        return (
            self.background.pressured(ignore_backlog=bool(self.daily.capacity_pending))
            and not self.daily.recording.storage_error
        )

    async def _compact(self, recording_id: str) -> None:
        if self._compaction_pressured():
            self.daily.recording.diagnostics.count("compaction_deferred")
            return
        async with self._file_lock(recording_id):
            await self._compact_locked(recording_id)

    async def _compact_files(self, folder):
        def enumerate_files():
            files = []
            for parent, directories, names in os.walk(folder, followlinks=False):
                files.extend(Path(parent) / name for name in names)
                files.extend(
                    Path(parent) / name
                    for name in directories
                    if (Path(parent) / name).is_symlink()
                )
            return files

        files = await asyncio.to_thread(enumerate_files)
        for start in range(0, len(files), 64):
            if self._compaction_pressured():
                self.daily.recording.diagnostics.count("compaction_deferred")
                return False
            batch = files[start : start + 64]

            def remove(batch=batch):
                self.daily.recording.storage_guard()
                for path in batch:
                    path.unlink(missing_ok=True)

            await self._file_work(remove)
            await asyncio.sleep(0.01)
            if self._compaction_pressured():
                return False
        await self._file_work(lambda: self._remove_folder(folder))
        return True

    async def _compact_locked(self, recording_id: str) -> None:
        if self._has_job(recording_id):
            return
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            if day["status"] in ("deleted", "expired"):
                return
            if day.get("archive_state") != "mp4":
                return
            if self.db.execute(
                "SELECT 1 FROM clip_recording_pins WHERE day_id=? LIMIT 1", (recording_id,)
            ).fetchone():
                return
            master = self._path(recording_id)
            if not master.is_file() or master.stat().st_size != day["size_bytes"]:
                segments = self.daily.catalog.segments(recording_id, ready_only=True)
                intact = bool(segments) and all(
                    (self.root / part["path"]).is_file()
                    and (self.root / part["path"]).stat().st_size == part["size_bytes"]
                    for part in segments
                )
                if intact:
                    await storage_io(master.unlink, missing_ok=True)
                    day.update(
                        archive_state="segments",
                        export_state="failed",
                        download_path=None,
                        size_bytes=None,
                        export_expires_at=None,
                        archive_retry_at=iso(self.daily.clock() + 30),
                        error={
                            "code": "STORAGE_ERROR",
                            "message": "Rebuilding damaged archive from retained sources.",
                        },
                    )
                    await self._commit_workspace_release(recording_id, day=day)
                return
            # New admissions now use MP4. Old snapshot leases prevented reaching this point.
            uuid.UUID(recording_id)
            folder = self.root / "daily" / recording_id
            if not folder.resolve().is_relative_to((self.root / "daily").resolve()):
                raise OSError("Invalid compaction path")
        if not await self._compact_files(folder):
            return
        async with self.lock:
            await self._commit_workspace_release(recording_id)

    async def delete_day(self, recording_id: str) -> None:
        async with self.daily.recording.thumbnail_deletion("day", recording_id):
            async with self.lock:
                day = self.daily.catalog.get_day(recording_id)
                if day["status"] in ("recording", "finalizing"):
                    raise ApiError(409, "RECORDING_ACTIVE", "Current day cannot be deleted.")
                if self._has_job(recording_id):
                    raise ApiError(409, "EXPORT_IN_PROGRESS", "Archive is in progress.")
                if self.db.execute(
                    "SELECT 1 FROM clip_recording_pins WHERE day_id=? LIMIT 1", (recording_id,)
                ).fetchone():
                    raise ApiError(
                        409, "EXPORT_IN_PROGRESS", "A media request uses this recording."
                    )
                if day.get("cleanup_state") == "complete":
                    return
                if day["status"] != "expired":
                    day.update(
                        status="deleted",
                        deleted_at=day.get("deleted_at") or iso(self.daily.clock()),
                    )
                day.update(
                    playback_state="unavailable",
                    playback_path=None,
                    download_path=None,
                    export_allowed=False,
                    size_bytes=None,
                    export_expires_at=None,
                    cleanup_state="pending",
                    cleanup_completed_at=None,
                    cleanup_error=None,
                    space_reclaimed=None,
                )
                async with self.daily.recording.transaction():
                    self.daily.catalog.save_day(day)
                    self.db.execute(
                        "INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (recording_id,)
                    )
            if not await self._cleanup(recording_id):
                raise ApiError(409, "EXPORT_IN_PROGRESS", "A media request uses this recording.")

    async def _cleanup(self, recording_id: str) -> bool:
        async with self._file_lock(recording_id):
            return await self._cleanup_locked(recording_id)

    async def _cleanup_locked(self, recording_id: str) -> bool:
        if self._has_job(recording_id):
            return False
        async with self.lock:
            if self.db.execute(
                "SELECT 1 FROM clip_recording_pins WHERE day_id=? LIMIT 1", (recording_id,)
            ).fetchone():
                return False
            day = self.daily.catalog.get_day(recording_id)
            if day.get("cleanup_state") == "complete":
                self._release_workspace(recording_id)
                return True
            day.update(
                cleanup_state="pending",
                cleanup_completed_at=None,
                cleanup_error=None,
                space_reclaimed=None,
            )
            async with self.daily.recording.transaction():
                self.daily.catalog.save_day(day)
                self.db.execute("INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (recording_id,))
        uuid.UUID(recording_id)

        def remove():
            for category in ("daily", "daily-playback"):
                folder = self.root / category / recording_id
                if not folder.resolve().is_relative_to((self.root / category).resolve()):
                    raise OSError("Invalid cleanup path")
                self._remove_folder(folder)
            for suffix in (".mp4", ".tmp.mp4", ".ffconcat"):
                path = self.output / (recording_id + suffix)
                if not path.resolve().is_relative_to(self.output.resolve()):
                    raise OSError("Invalid media path")
                path.unlink(missing_ok=True)

            thumbnail = self.root / "daily-thumbnails" / (recording_id + ".jpg")
            if thumbnail.parent.resolve() != thumbnail.parent:
                raise OSError("Invalid thumbnail cleanup root")
            thumbnail.unlink(missing_ok=True)

        try:
            await self._file_work(remove)
        except OSError:
            async with self.lock:
                day = self.daily.catalog.get_day(recording_id)
                day.update(
                    cleanup_state="failed",
                    cleanup_completed_at=None,
                    cleanup_error={"code": "STORAGE_ERROR", "message": "Recording cleanup failed."},
                    space_reclaimed=None,
                )
                async with self.daily.recording.transaction():
                    self.daily.catalog.save_day(day)
            raise
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            day.update(
                cleanup_state="complete",
                cleanup_completed_at=iso(self.daily.clock()),
                cleanup_error=None,
                # Open handles can retain physical blocks after path removal.
                space_reclaimed=None,
            )
            await self._commit_workspace_release(recording_id, day=day, cleanup=True)
        return True

    async def refresh_retention(self, now: float) -> None:
        """Only update metadata on HTTP requests; file work belongs to the background worker."""
        async with self.lock:
            rows = self.db.execute(
                "SELECT id FROM daily_recordings "
                "WHERE status NOT IN ('recording','finalizing','deleted','expired')"
            ).fetchall()
            async with self.daily.recording.transaction():
                for row in rows:
                    state = self.daily.catalog.get_state(row[0])
                    day = state
                    if day["expires_at"] and epoch(day["expires_at"]) <= now:
                        day = self.daily.catalog.get_day(row[0])
                        day.update(
                            status="expired",
                            playback_state="unavailable",
                            playback_path=None,
                            download_path=None,
                            export_allowed=False,
                            size_bytes=None,
                            cleanup_state="pending",
                            cleanup_completed_at=None,
                            cleanup_error=None,
                            space_reclaimed=None,
                        )
                        self.daily.catalog.save_day(day)
                        self.db.execute(
                            "INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (day["recording_id"],)
                        )

    async def maintenance(self, now: float) -> None:
        guard = getattr(self.daily.recording, "storage_guard", None)
        if guard is not None:
            await asyncio.to_thread(guard)
        async with self.maintenance_lock:
            await self.refresh_retention(now)
            await self.playback.maintenance(now)
            async with self.lock:
                pending = [r[0] for r in self.db.execute("SELECT day_id FROM daily_cleanup")]
                compact = [r[0] for r in self.db.execute("SELECT day_id FROM daily_compactions")]
            for day_id in pending:
                try:
                    await self._cleanup(day_id)
                except OSError:
                    LOG.exception("Retrying daily cleanup: %s", day_id)
            for day_id in compact:
                if not getattr(self.settings, "daily_archive_enabled", True):
                    break
                try:
                    await self._compact(day_id)
                except OSError:
                    LOG.exception("Retrying source compaction: %s", day_id)
            if (
                self.closed
                or self.jobs
                or not getattr(self.settings, "daily_archive_enabled", True)
            ):
                return
            async with self.lock:
                candidates = [
                    self.daily.catalog.get_state(r[0])
                    for r in self.db.execute(
                        "SELECT id FROM daily_recordings WHERE status='closed' ORDER BY date DESC"
                    )
                ]
            for day in candidates:
                if day.get("archive_state") == "mp4" or day["recorded_seconds"] <= 0:
                    continue
                retry = day.get("archive_retry_at")
                if retry and epoch(retry) > now:
                    continue
                try:
                    await self._enqueue(day["recording_id"])
                except ApiError as exc:
                    async with self.lock:
                        current = self.daily.catalog.get_day(day["recording_id"])
                        if current["status"] == "closed":
                            current.update(
                                export_state="failed",
                                error={"code": exc.code, "message": "Automatic archive deferred."},
                                archive_retry_at=iso(now + 30),
                            )
                            async with self.daily.recording.transaction():
                                self.daily.catalog.save_day(current)
                    # Avoid repeatedly reserving space for every old day on a full disk.
                    break
                if self.jobs:
                    break

    async def start(self) -> None:
        await self.maintenance(self.daily.clock())
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(5)
            try:
                await self.maintenance(self.daily.clock())
            except (OSError, RuntimeError, ApiError, sqlite3.Error):
                LOG.exception("Automatic daily archival maintenance failed")

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        keys = list(self.jobs)
        tasks = [*self.jobs.values(), *([self.task] if self.task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self.lock:
            async with self.daily.recording.transaction():
                for key in keys:
                    day = self.daily.catalog.get_day(key)
                    if day["export_state"] == "processing":
                        day.update(
                            export_state="failed",
                            archive_retry_at=None,
                            error={"code": "JOB_INTERRUPTED", "message": "Archive interrupted."},
                        )
                        self.daily.catalog.save_day(day)
                        row = self.db.execute(
                            "SELECT body FROM daily_exports WHERE day_id=?", (key,)
                        ).fetchone()
                        if row:
                            job = json.loads(row[0])
                            job.update(status="failed", error=day["error"])
                            self.db.execute(
                                "UPDATE daily_exports SET status='failed',body=? WHERE day_id=?",
                                (json.dumps(job), key),
                            )
                    self.reservations.pop(key, None)
                    self.reservation_outputs.pop(key, None)
        await self.playback.close()
