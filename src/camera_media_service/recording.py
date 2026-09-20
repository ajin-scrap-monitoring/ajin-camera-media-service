"""Durable clip catalog and bounded, disk-backed received-frame buffer."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub, RecordingQueue

LOG = logging.getLogger(__name__)
DEFAULT_POLICY = {"pre_roll_seconds": 30, "post_roll_seconds": 30, "retention_days": 30}
ACTIVE = ("collecting", "processing")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def iso(value: float) -> str:
    return (
        datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def validate_policy(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise ApiError(400, "INVALID_ARGUMENT", "All recording policy fields are required.")
    for key, maximum in (
        ("pre_roll_seconds", 300),
        ("post_roll_seconds", 300),
        ("retention_days", 365),
    ):
        minimum = 1 if key == "retention_days" else 0
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ApiError(400, "INVALID_ARGUMENT", f"{key} must be {minimum}..{maximum}.")
    if value["pre_roll_seconds"] + value["post_roll_seconds"] < 1:
        raise ApiError(400, "INVALID_ARGUMENT", "The recording interval must be positive.")
    return dict(value)


class RecordingService:
    def __init__(
        self,
        root: Path,
        cameras: set[str],
        hub: LiveFrameHub,
        *,
        ffmpeg: str = "ffmpeg",
        max_buffer_bytes: int = 2 * 1024**3,
        max_jobs: int = 32,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.buffer = self.root / "buffer"
        self.clips = self.root / "clips"
        self.buffer.mkdir(exist_ok=True)
        self.clips.mkdir(exist_ok=True)
        self.cameras, self.hub = cameras, hub
        self.ffmpeg = ffmpeg
        self.max_buffer_bytes, self.max_jobs = max_buffer_bytes, max_jobs
        self.db = sqlite3.connect(self.root / "catalog.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS policies(camera TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, event TEXT NOT NULL,
                accepted REAL NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                status TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(camera,event));
            CREATE TABLE IF NOT EXISTS frames(
                path TEXT PRIMARY KEY, camera TEXT NOT NULL, timestamp REAL NOT NULL,
                size INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS frame_time ON frames(camera,timestamp);
            CREATE INDEX IF NOT EXISTS job_time ON jobs(accepted,id);
            CREATE INDEX IF NOT EXISTS job_status ON jobs(status);
            CREATE TABLE IF NOT EXISTS pending_cleanup(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS service_metadata(key TEXT PRIMARY KEY, value BLOB NOT NULL);
        """)
        # Internal cursor integrity only; no shared API credential or operator configuration.
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO service_metadata VALUES ('cursor_key', ?)",
                (secrets.token_bytes(32),),
            )
        self.cursor_key = self.db.execute(
            "SELECT value FROM service_metadata WHERE key='cursor_key'"
        ).fetchone()[0]
        self.lock = asyncio.Lock()
        self.encoding_slot = asyncio.Semaphore(1)
        self.queue = None
        self.tasks: list[asyncio.Task] = []
        self.jobs: dict[str, asyncio.Task] = {}
        self.storage_error = False
        self._closed = False
        # A buffer write or encoder may have been interrupted. Never imply success.
        with self.db:
            for row in self.db.execute("SELECT body FROM jobs WHERE status IN (?,?)", ACTIVE):
                body = json.loads(row["body"])
                body["status"] = "failed"
                body["error"] = {"code": "JOB_INTERRUPTED", "message": "Service restarted."}
                self._save(body)
            self.db.execute(
                "INSERT OR IGNORE INTO pending_cleanup SELECT id FROM jobs "
                "WHERE status IN ('expired','failed')"
            )
        # Only service-generated files inside its private directories are touched.
        referenced = {r[0] for r in self.db.execute("SELECT path FROM frames")}
        for path in self.buffer.glob("*.jpg"):
            if path.name not in referenced:
                path.unlink(missing_ok=True)

    def _camera(self, camera: str) -> None:
        if camera not in self.cameras:
            raise ApiError(404, "CAMERA_NOT_FOUND", "Camera is not registered.")

    def _save(self, body: dict) -> None:
        self.db.execute(
            "UPDATE jobs SET status=?,body=? WHERE id=?",
            (body["status"], json.dumps(body), body["clip_id"]),
        )

    def _get(self, clip_id: str) -> dict:
        row = self.db.execute("SELECT body FROM jobs WHERE id=?", (clip_id,)).fetchone()
        if row is None:
            raise ApiError(404, "CLIP_NOT_FOUND", "Clip does not exist.")
        return json.loads(row[0])

    def _policy(self, camera: str) -> dict:
        row = self.db.execute("SELECT body FROM policies WHERE camera=?", (camera,)).fetchone()
        return json.loads(row[0]) if row else dict(DEFAULT_POLICY)

    async def get_policy(self, camera: str) -> dict:
        self._camera(camera)
        async with self.lock:
            return self._policy(camera)

    async def set_policy(self, camera: str, value: object) -> dict:
        self._camera(camera)
        policy = validate_policy(value)
        async with self.lock:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO policies VALUES (?,?)", (camera, json.dumps(policy))
                )
        return policy

    async def create_clip(self, camera: str, event: str) -> tuple[dict, bool]:
        if (
            not isinstance(camera, str)
            or not isinstance(event, str)
            or not event.strip()
            or len(event) > 128
        ):
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid camera_id or event_id.")
        self._camera(camera)
        async with self.lock:
            row = self.db.execute(
                "SELECT body FROM jobs WHERE camera=? AND event=?", (camera, event)
            ).fetchone()
            if row:
                body = json.loads(row[0])
                await self._expire(body)
                return body, False
            if self.storage_error:
                raise ApiError(503, "STORAGE_UNAVAILABLE", "Frame storage is unavailable.")
            count = self.db.execute(
                "SELECT count(*) FROM jobs WHERE status IN (?,?)", ACTIVE
            ).fetchone()[0]
            if count >= self.max_jobs:
                raise ApiError(503, "SERVICE_BUSY", "Too many active recordings.")
            now = int(time.time() * 1000) / 1000
            policy = self._policy(camera)
            start, end = now - policy["pre_roll_seconds"], now + policy["post_roll_seconds"]
            clip_id = str(uuid.uuid4())
            body = dict(
                clip_id=clip_id,
                camera_id=camera,
                event_id=event,
                status="collecting",
                coverage=None,
                accepted_at=iso(now),
                requested_start_at=iso(start),
                requested_end_at=iso(end),
                recording_policy=policy,
                actual_start_at=None,
                actual_end_at=None,
                gaps=[],
                completed_at=None,
                expires_at=None,
                deleted_at=None,
                content_path=None,
                error=None,
            )
            with self.db:
                self.db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                    (clip_id, camera, event, now, start, end, body["status"], json.dumps(body)),
                )
            return body, True

    async def get_clip(self, clip_id: str) -> dict:
        async with self.lock:
            body = self._get(clip_id)
            await self._expire(body)
            return body

    async def list_clips(self, filters: dict, limit: int, after: tuple | None) -> list[dict]:
        if "camera_id" in filters:
            self._camera(filters["camera_id"])
        clauses, args = [], []
        for key, column in (("camera_id", "camera"), ("event_id", "event"), ("status", "status")):
            if key in filters:
                clauses.append(f"{column}=?")
                args.append(filters[key])
        if "status" not in filters:
            clauses.append("status NOT IN ('deleted','expired')")
        for key, op in (("from", ">="), ("to", "<")):
            if key in filters:
                clauses.append(f"accepted {op} ?")
                args.append(filters[key])
        if after:
            clauses.append("(accepted < ? OR (accepted = ? AND id < ?))")
            args.extend((after[0], after[0], after[1]))
        async with self.lock:
            await self._expire_all()
            sql = "SELECT body FROM jobs"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY accepted DESC,id DESC LIMIT ?"
            return [json.loads(r[0]) for r in self.db.execute(sql, (*args, limit))]

    async def delete_clip(self, clip_id: str) -> None:
        async with self.lock:
            body = self._get(clip_id)
            await self._expire(body)
            if body["status"] in ACTIVE:
                raise ApiError(409, "CLIP_IN_PROGRESS", "Recording is still in progress.")
            if body["status"] in ("deleted", "expired"):
                return
            await self._remove_files(clip_id)
            body.update(status="deleted", deleted_at=iso(time.time()), content_path=None)
            with self.db:
                self._save(body)

    async def _remove_files(self, clip_id: str) -> None:
        # clip_id comes from the catalog, never directly from a supplied filesystem path.
        def remove():
            for suffix in (".mp4", ".tmp.mp4", ".ffconcat"):
                (self.clips / f"{clip_id}{suffix}").unlink(missing_ok=True)

        await asyncio.to_thread(remove)

    async def _expire(self, body: dict) -> None:
        if body["status"] == "ready" and epoch(body["expires_at"]) <= time.time():
            # Deny access immediately, even when physical cleanup must be retried.
            body.update(status="expired", content_path=None)
            with self.db:
                self._save(body)
                self.db.execute(
                    "INSERT OR IGNORE INTO pending_cleanup VALUES (?)", (body["clip_id"],)
                )
        if body["status"] == "expired":
            await self._cleanup_pending(body["clip_id"])

    async def _cleanup_pending(self, clip_id: str) -> None:
        if self.db.execute("SELECT 1 FROM pending_cleanup WHERE id=?", (clip_id,)).fetchone():
            try:
                await self._remove_files(clip_id)
                with self.db:
                    self.db.execute("DELETE FROM pending_cleanup WHERE id=?", (clip_id,))
            except OSError:
                LOG.exception("Clip cleanup failed")

    async def _expire_all(self) -> None:
        rows = self.db.execute(
            "SELECT body FROM jobs WHERE status='ready' AND json_extract(body,'$.expires_at')<=?",
            (iso(time.time()),),
        ).fetchall()
        for row in rows:
            await self._expire(json.loads(row[0]))
        for row in self.db.execute("SELECT id FROM pending_cleanup").fetchall():
            await self._cleanup_pending(row[0])

    async def store_frame(self, frame: FrameEnvelope) -> None:
        async with self.lock:
            await self._prune(time.time(), reserve=len(frame.jpeg))
            size = self.db.execute("SELECT coalesce(sum(size),0) FROM frames").fetchone()[0]
            if size + len(frame.jpeg) > self.max_buffer_bytes:
                raise OSError("Pinned frames exhausted the buffer budget")
            name = f"{uuid.uuid4()}.jpg"
            path = self.buffer / name
            try:
                await asyncio.to_thread(path.write_bytes, frame.jpeg)
                with self.db:
                    self.db.execute(
                        "INSERT INTO frames VALUES (?,?,?,?)",
                        (name, frame.camera_id, frame.received_at, len(frame.jpeg)),
                    )
            except BaseException:
                path.unlink(missing_ok=True)
                raise

    async def _prune(self, now: float, reserve: int = 0) -> None:
        total = self.db.execute("SELECT coalesce(sum(size),0) FROM frames").fetchone()[0]
        cutoff = now - 300 if total + reserve <= self.max_buffer_bytes else float("inf")
        rows = self.db.execute(
            """
            SELECT * FROM frames f WHERE timestamp < ? AND NOT EXISTS (
                SELECT 1 FROM jobs j WHERE j.camera=f.camera
                AND j.status IN ('collecting','processing')
                AND f.timestamp >= j.start AND f.timestamp < j.end)
            ORDER BY timestamp
        """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            if row["timestamp"] >= now - 300 and total + reserve <= self.max_buffer_bytes:
                break
            await asyncio.to_thread((self.buffer / row["path"]).unlink, missing_ok=True)
            with self.db:
                self.db.execute("DELETE FROM frames WHERE path=?", (row["path"],))
            total -= row["size"]

    async def process_clip(self, clip_id: str) -> None:
        from camera_media_service.encoding import encode_clip

        async with self.encoding_slot:
            # Include received frames still awaiting disk I/O before freezing the interval.
            # Only receipts present now are included; continuous later input cannot starve the job.
            if self.queue is not None:
                await self.queue.wait_received()
            async with self.lock:
                body = self._get(clip_id)
                if body["status"] != "collecting":
                    return
                start, end = epoch(body["requested_start_at"]), epoch(body["requested_end_at"])
                if time.time() < end:
                    return
                frames = self.db.execute(
                    "SELECT * FROM frames WHERE camera=? AND timestamp>=? AND timestamp<? "
                    "ORDER BY timestamp,path",
                    (body["camera_id"], start, end),
                ).fetchall()
                times = [r["timestamp"] for r in frames]
                points = [start, *times, end]
                gaps = [
                    {"start_at": iso(a), "end_at": iso(b)}
                    for a, b in zip(points, points[1:], strict=False)
                    if b - a > 1
                ]
                body.update(
                    status="processing",
                    coverage="partial" if gaps else "complete",
                    gaps=gaps,
                    actual_start_at=iso(times[0]) if times else None,
                    actual_end_at=iso(times[-1]) if times else None,
                )
                if not frames:
                    body.update(
                        status="failed",
                        coverage="none",
                        gaps=[{"start_at": iso(start), "end_at": iso(end)}],
                        error={"code": "NO_FRAMES_AVAILABLE", "message": "No frames received."},
                    )
                with self.db:
                    self._save(body)
                if not frames:
                    return
            try:
                await encode_clip(
                    self.ffmpeg,
                    self.buffer,
                    self.clips,
                    clip_id,
                    [(r["path"], r["timestamp"]) for r in frames],
                )
                now = time.time()
                body.update(
                    status="ready",
                    completed_at=iso(now),
                    expires_at=iso(now + body["recording_policy"]["retention_days"] * 86400),
                    content_path=f"/api/v1/clips/{clip_id}/content",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.exception("Clip encoding failed: %s", clip_id)
                body.update(
                    status="failed",
                    error={
                        "code": "STORAGE_UNAVAILABLE"
                        if isinstance(exc, OSError)
                        else "ENCODING_FAILED",
                        "message": "Clip creation failed.",
                    },
                )
            while True:
                try:
                    async with self.lock:
                        with self.db:
                            self._save(body)
                    break
                except sqlite3.Error:
                    LOG.exception("Retrying clip result persistence: %s", clip_id)
                    await asyncio.sleep(1)

    async def start(self) -> None:
        self.queue = self.hub.subscribe(maxsize=100, recording=True)
        assert isinstance(self.queue, RecordingQueue)
        self.tasks = [asyncio.create_task(self._writer()), asyncio.create_task(self._scheduler())]

    async def _writer(self) -> None:
        assert self.queue is not None
        while True:
            frame = await self.queue.get()
            try:
                await self.store_frame(frame)
                self.storage_error = False
            except (OSError, sqlite3.Error):
                self.storage_error = True
                LOG.exception("Recording frame storage failed")
            finally:
                self.queue.acknowledge(frame)

    async def _scheduler(self) -> None:
        maintenance_at = 0.0
        while True:
            await asyncio.sleep(0.25)
            try:
                async with self.lock:
                    if time.monotonic() >= maintenance_at:
                        await self._expire_all()
                        await self._prune(time.time())
                        maintenance_at = time.monotonic() + 5
                    rows = self.db.execute(
                        "SELECT id FROM jobs WHERE status='collecting' AND end<=?",
                        (time.time() - 0.25,),
                    ).fetchall()
                for row in rows:
                    if row[0] not in self.jobs:
                        task = asyncio.create_task(self.process_clip(row[0]))
                        self.jobs[row[0]] = task
                        task.add_done_callback(lambda done, key=row[0]: self._job_done(key, done))
            except (OSError, sqlite3.Error):
                LOG.exception("Recording maintenance failed")

    def _job_done(self, key: str, task: asyncio.Task) -> None:
        self.jobs.pop(key, None)
        if not task.cancelled() and task.exception():
            LOG.error("Recording task failed", exc_info=task.exception())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.queue is not None:
            self.hub.unsubscribe(self.queue)
        tasks = [*self.tasks, *self.jobs.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
