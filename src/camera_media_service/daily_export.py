"""Automatic canonical daily MP4 archives, retention and safe source compaction."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import uuid

from camera_media_service.archive_playback import ArchivedPlaybackService
from camera_media_service.daily_recording import DailyRecordingService
from camera_media_service.encoding import merge_daily_mp4, verify_archive
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
        self.task = None
        self.closed = False
        self.reservations = daily.storage_reservations
        self.playback = ArchivedPlaybackService(daily)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_cleanup(day_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS daily_compactions(day_id TEXT PRIMARY KEY);
        """)
        with self.db:
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
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            self._exportable(day)
            if day.get("archive_state") == "mp4" or self._has_job(recording_id):
                return
            # One archive at a time also bounds temporary duplicate disk reservations.
            if self.jobs:
                return
            segments = self.daily.catalog.segments(recording_id, ready_only=True)
            reuse = self._path(recording_id).is_file()
            reserve = 0 if reuse else int(sum(s["size_bytes"] for s in segments) * 1.2) + 1048576
        if reserve:
            await self.daily.check_space(reserve)
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            self._exportable(day)
            if self.closed or self.jobs or day.get("archive_state") == "mp4":
                return
            job = {
                "job_id": str(uuid.uuid4()),
                "recording_id": recording_id,
                "revision": day["revision"],
                "status": "processing",
                "error": None,
            }
            with self.db:
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
            task = asyncio.create_task(self._export(job, segments, reuse=reuse))
            self.jobs[recording_id] = task
            task.add_done_callback(lambda done, key=recording_id: self._done(key, done))

    def _done(self, key, task):
        if self.jobs.get(key) is task:
            self.jobs.pop(key, None)
            self.reservations.pop(key, None)
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
        recording_id = job["recording_id"]
        final = self._path(recording_id)
        error, metadata = None, None
        try:
            async with self.slot:
                if not reuse:
                    await self.daily.check_space()
                    await merge_daily_mp4(
                        self.daily.recording.ffmpeg,
                        segments,
                        self.output / (recording_id + ".tmp.mp4"),
                        final,
                    )
                metadata = await verify_archive(self.daily.recording.ffmpeg, final, segments)
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
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            if day["status"] in ("expired", "deleted") or (
                day["expires_at"] and epoch(day["expires_at"]) <= self.daily.clock()
            ):
                error = {"code": "RECORDING_EXPIRED", "message": "Day expired during archive."}
            with self.db:
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
                    offset_us = 0
                    for segment in segments:
                        segment.update(
                            archive_path=str(final.relative_to(self.root)),
                            archive_offset=offset_us / 1_000_000,
                        )
                        self.db.execute(
                            "UPDATE daily_segments SET body=? WHERE id=?",
                            (json.dumps(segment), segment["segment_id"]),
                        )
                        # Match the concat demuxer's per-entry microsecond durations.
                        offset_us += round(segment["duration"] * 1_000_000)
                    day.update(
                        archive_state="mp4",
                        export_state="ready",
                        size_bytes=metadata["size_bytes"],
                        download_path=f"/api/v2/daily-recordings/{recording_id}/content",
                        export_expires_at=day["expires_at"],
                        archive_retry_at=None,
                        error=None,
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO daily_compactions VALUES(?)", (recording_id,)
                    )
                self.daily.catalog.save_day(day)
        if error:
            final.unlink(missing_ok=True)

    async def _compact(self, recording_id: str) -> None:
        if self._has_job(recording_id):
            return
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
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
                    with self.db:
                        self.daily.catalog.save_day(day)
                        self.db.execute(
                            "DELETE FROM daily_compactions WHERE day_id=?", (recording_id,)
                        )
                    master.unlink(missing_ok=True)
                return
            # New admissions now use MP4. Old snapshot leases prevented reaching this point.
            uuid.UUID(recording_id)
            folder = self.root / "daily" / recording_id
            if not folder.resolve().is_relative_to((self.root / "daily").resolve()):
                raise OSError("Invalid compaction path")
        if folder.exists():
            await asyncio.to_thread(shutil.rmtree, folder)
        async with self.lock:
            with self.db:
                self.db.execute("DELETE FROM daily_compactions WHERE day_id=?", (recording_id,))

    async def delete_day(self, recording_id: str) -> None:
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            if day["status"] in ("recording", "finalizing"):
                raise ApiError(409, "RECORDING_ACTIVE", "Current day cannot be deleted.")
            if self._has_job(recording_id):
                raise ApiError(409, "EXPORT_IN_PROGRESS", "Archive is in progress.")
            if self.db.execute(
                "SELECT 1 FROM clip_recording_pins WHERE day_id=? LIMIT 1", (recording_id,)
            ).fetchone():
                raise ApiError(409, "EXPORT_IN_PROGRESS", "A media request uses this recording.")
            if day["status"] != "expired":
                day.update(status="deleted", deleted_at=iso(self.daily.clock()))
            day.update(
                playback_state="unavailable",
                playback_path=None,
                download_path=None,
                export_allowed=False,
                size_bytes=None,
                export_expires_at=None,
            )
            with self.db:
                self.daily.catalog.save_day(day)
                self.db.execute("INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (recording_id,))
        await self._cleanup(recording_id)

    async def _cleanup(self, recording_id: str) -> None:
        if self._has_job(recording_id):
            return
        async with self.lock:
            if self.db.execute(
                "SELECT 1 FROM clip_recording_pins WHERE day_id=? LIMIT 1", (recording_id,)
            ).fetchone():
                return
        uuid.UUID(recording_id)

        def remove():
            for category in ("daily", "daily-playback"):
                folder = self.root / category / recording_id
                if not folder.resolve().is_relative_to((self.root / category).resolve()):
                    raise OSError("Invalid cleanup path")
                if folder.exists():
                    shutil.rmtree(folder)
            for suffix in (".mp4", ".tmp.mp4", ".ffconcat"):
                path = self.output / (recording_id + suffix)
                if not path.resolve().is_relative_to(self.output.resolve()):
                    raise OSError("Invalid media path")
                path.unlink(missing_ok=True)

        await asyncio.to_thread(remove)
        async with self.lock:
            with self.db:
                self.db.execute("DELETE FROM daily_cleanup WHERE day_id=?", (recording_id,))
                self.db.execute("DELETE FROM daily_compactions WHERE day_id=?", (recording_id,))

    async def refresh_retention(self, now: float) -> None:
        """Only update metadata on HTTP requests; file work belongs to the background worker."""
        async with self.lock:
            rows = self.db.execute(
                "SELECT body FROM daily_recordings "
                "WHERE status NOT IN ('recording','finalizing','deleted','expired')"
            ).fetchall()
            with self.db:
                for row in rows:
                    day = json.loads(row[0])
                    if day["expires_at"] and epoch(day["expires_at"]) <= now:
                        day.update(
                            status="expired",
                            playback_state="unavailable",
                            playback_path=None,
                            download_path=None,
                            export_allowed=False,
                            size_bytes=None,
                        )
                        self.daily.catalog.save_day(day)
                        self.db.execute(
                            "INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (day["recording_id"],)
                        )

    async def maintenance(self, now: float) -> None:
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
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT body FROM daily_recordings WHERE status='closed' ORDER BY date DESC"
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
                            with self.db:
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
            with self.db:
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
        await self.playback.close()
