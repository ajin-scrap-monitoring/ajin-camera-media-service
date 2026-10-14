"""Bounded daily MP4 jobs, retention and isolated daily-media deletion."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import uuid

from camera_media_service.daily_recording import DailyRecordingService
from camera_media_service.encoding import merge_daily_mp4
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
        self.task = None
        self.closed = False
        self.reservations = daily.storage_reservations
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_cleanup(day_id TEXT PRIMARY KEY)")
        with self.db:
            for row in self.db.execute(
                "SELECT day_id,body FROM daily_exports WHERE status='processing'"
            ).fetchall():
                job = json.loads(row["body"])
                job.update(
                    status="failed",
                    error={"code": "JOB_INTERRUPTED", "message": "Export interrupted by restart."},
                )
                self.db.execute(
                    "UPDATE daily_exports SET status=?,body=? WHERE day_id=?",
                    ("failed", json.dumps(job), row["day_id"]),
                )
                day = daily.catalog.get_day(row["day_id"])
                day.update(export_state="failed", error=job["error"])
                daily.catalog.save_day(day)
                (self.output / (row["day_id"] + ".tmp.mp4")).unlink(missing_ok=True)
                (self.output / (row["day_id"] + ".ffconcat")).unlink(missing_ok=True)

    def _accessible(self, day: dict) -> None:
        if day["status"] in ("deleted", "expired"):
            raise ApiError(
                410, "DAILY_RECORDING_" + day["status"].upper(), "Recording is no longer available."
            )

    def _exportable(self, day: dict) -> None:
        self._accessible(day)
        if self.daily.clock() < epoch(day["window_end_at"]):
            raise ApiError(409, "DAY_NOT_CLOSED", "Today cannot be exported.")
        if day["status"] == "finalizing":
            raise ApiError(409, "DAY_FINALIZING", "Day is being finalized.")
        if day["status"] != "closed":
            raise ApiError(409, "DAY_NOT_CLOSED", "Day is not closed.")
        if day["recorded_seconds"] <= 0:
            raise ApiError(409, "NO_RECORDING_DATA", "No saved media.")

    async def request_export(self, recording_id: str) -> tuple[dict, bool]:
        await self.maintenance(self.daily.clock())
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            self._exportable(day)
            if day["export_state"] == "ready" and (self.output / (recording_id + ".mp4")).is_file():
                return day, False
            if recording_id in self.jobs:
                return day, False
            if len(self.jobs) >= self.settings.max_daily_export_jobs:
                raise ApiError(503, "EXPORT_CAPACITY_EXCEEDED", "Export queue is full.")
            segments = self.daily.catalog.segments(recording_id, ready_only=True)
            reserve = int(sum(s["size_bytes"] for s in segments) * 1.2) + 1048576
            await self.daily.check_space(reserve)
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
                    error=None,
                )
                self.daily.catalog.save_day(day)
            self.reservations[recording_id] = reserve
            task = asyncio.create_task(self._export(job, segments))
            self.jobs[recording_id] = task
            task.add_done_callback(lambda done, key=recording_id: self._done(key, done))
            return day, True

    def _done(self, key, task):
        if self.jobs.get(key) is task:
            self.jobs.pop(key, None)
            self.reservations.pop(key, None)
        if not task.cancelled() and task.exception():
            LOG.error("Daily export failed", exc_info=task.exception())

    async def _export(self, job: dict, segments: list[dict]) -> None:
        recording_id = job["recording_id"]
        error = None
        metadata = None
        try:
            async with self.slot:
                await self.daily.check_space()
                metadata = await merge_daily_mp4(
                    self.daily.recording.ffmpeg,
                    segments,
                    self.output / (recording_id + ".tmp.mp4"),
                    self.output / (recording_id + ".mp4"),
                )
        except asyncio.CancelledError:
            error = {"code": "JOB_INTERRUPTED", "message": "Export interrupted."}
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
            error = {"code": code, "message": "Daily MP4 preparation failed."}
            LOG.exception("Daily MP4 failed: %s", recording_id)
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            if day["status"] in ("expired", "deleted"):
                error = {"code": "RECORDING_EXPIRED", "message": "Day expired during export."}
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
                        error=error,
                    )
                else:
                    day.update(
                        export_state="ready",
                        size_bytes=metadata["size_bytes"],
                        download_path=f"/api/v2/daily-recordings/{recording_id}/content",
                        export_expires_at=iso(
                            min(
                                epoch(day["expires_at"]),
                                self.daily.clock() + self.settings.daily_export_cache_hours * 3600,
                            )
                        ),
                        error=None,
                    )
                self.daily.catalog.save_day(day)
        if error:
            (self.output / (recording_id + ".mp4")).unlink(missing_ok=True)

    async def delete_day(self, recording_id: str) -> None:
        async with self.lock:
            day = self.daily.catalog.get_day(recording_id)
            if day["status"] in ("recording", "finalizing"):
                raise ApiError(409, "RECORDING_ACTIVE", "Current day cannot be deleted.")
            if recording_id in self.jobs:
                raise ApiError(409, "EXPORT_IN_PROGRESS", "Export is in progress.")
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
        if recording_id in self.jobs:
            return
        # Values originate from the catalog; verify resolved deletion stays in owned roots.
        uuid.UUID(recording_id)
        folder = self.root / "daily" / recording_id
        owned = (self.root / "daily").resolve()
        if not folder.resolve().is_relative_to(owned):
            raise OSError("Invalid cleanup path")

        def remove():
            if folder.exists():
                shutil.rmtree(folder)
            for suffix in (".mp4", ".tmp.mp4", ".ffconcat"):
                path = self.output / (recording_id + suffix)
                if not path.resolve().is_relative_to(self.output.resolve()):
                    raise OSError("Invalid cleanup path")
                path.unlink(missing_ok=True)

        await asyncio.to_thread(remove)
        async with self.lock:
            with self.db:
                self.db.execute("DELETE FROM daily_cleanup WHERE day_id=?", (recording_id,))

    async def maintenance(self, now: float) -> None:
        async with self.lock:
            rows = self.db.execute(
                "SELECT body FROM daily_recordings "
                "WHERE status NOT IN ('recording','finalizing','deleted','expired')"
            ).fetchall()
            for row in rows:
                day = json.loads(row[0])
                recording_id = day["recording_id"]
                if day["expires_at"] and epoch(day["expires_at"]) <= now:
                    day.update(
                        status="expired",
                        playback_state="unavailable",
                        playback_path=None,
                        download_path=None,
                        export_allowed=False,
                        size_bytes=None,
                    )
                    with self.db:
                        self.daily.catalog.save_day(day)
                        self.db.execute(
                            "INSERT OR IGNORE INTO daily_cleanup VALUES(?)", (recording_id,)
                        )
                elif (
                    day["export_expires_at"]
                    and epoch(day["export_expires_at"]) <= now
                    and recording_id not in self.jobs
                ):
                    (self.output / (recording_id + ".mp4")).unlink(missing_ok=True)
                    day.update(
                        export_state="not_requested",
                        download_path=None,
                        size_bytes=None,
                        export_expires_at=None,
                    )
                    with self.db:
                        self.daily.catalog.save_day(day)
            pending = [r[0] for r in self.db.execute("SELECT day_id FROM daily_cleanup")]
        for recording_id in pending:
            try:
                await self._cleanup(recording_id)
            except OSError:
                LOG.exception("Retrying daily cleanup: %s", recording_id)

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.maintenance(self.daily.clock())
            except (OSError, sqlite3.Error):
                LOG.exception("Daily retention unavailable")

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        tasks = [*self.jobs.values(), *([self.task] if self.task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
