"""Durable clip catalog and bounded, disk-backed received-frame buffer."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from camera_media_service.daily_models import StoredFrame
from camera_media_service.media_usage import iter_regular_files
from camera_media_service.persisted_media_info import close_probe_database, persist_generated_info
from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub, RecordingQueue
from camera_media_service.recording_checkpoint import CatalogCheckpointer
from camera_media_service.recording_diagnostics import RecordingDiagnostics, RecordingLock
from camera_media_service.storage_reservations import StorageReservations

LOG = logging.getLogger(__name__)
DEFAULT_POLICY = {"pre_roll_seconds": 30, "post_roll_seconds": 30, "retention_days": 30}
ACTIVE = ("collecting", "processing")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class StorageCapacityError(OSError):
    code = "INSUFFICIENT_STORAGE"

    def __init__(self, message, *, condition="buffer_capacity"):
        super().__init__(message)
        self.condition = condition


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
        storage_guard=None,
        max_storage_bytes: int | None = None,
        min_free_storage_bytes: int = 0,
    ):
        self.storage_guard = storage_guard or (lambda: None)
        self.storage_guard()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.buffer = self.root / "buffer"
        self.clips = self.root / "clips"
        self.buffer.mkdir(exist_ok=True)
        self.clips.mkdir(exist_ok=True)
        self.cameras, self.hub = cameras, hub
        self.ffmpeg = ffmpeg
        self.max_buffer_bytes, self.max_jobs = max_buffer_bytes, max_jobs
        self.max_storage_bytes = max_storage_bytes
        self.min_free_storage_bytes = min_free_storage_bytes
        self.capacity_check = None
        self.daily_frame_watermarks: dict[str, float] = {}
        self.thumbnail_observer = None
        self.thumbnail_change_observer = None
        self.thumbnail_pins = set()
        self._buffer_budget_snapshot = None
        self._budget_generation = 0
        self.capacity_reservations = StorageReservations(self._invalidate_budget_snapshot)
        self.capacity_outputs = {}
        # Catalogue users serialize through lock. Large daily reads and FULL
        # commits may run in a drained worker while retaining that same lock.
        self.db = sqlite3.connect(self.root / "catalog.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("PRAGMA journal_size_limit=8388608")
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
            CREATE INDEX IF NOT EXISTS frame_timestamp ON frames(timestamp);
            CREATE INDEX IF NOT EXISTS job_time ON jobs(accepted,id);
            CREATE INDEX IF NOT EXISTS job_status ON jobs(status);
            CREATE TABLE IF NOT EXISTS pending_cleanup(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS frame_pins(
                owner TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(owner,path));
            CREATE INDEX IF NOT EXISTS frame_pin_path ON frame_pins(path);
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
        self.diagnostics = RecordingDiagnostics()
        self.hub.recording_diagnostics = self.diagnostics
        self.lock = RecordingLock(self.diagnostics)
        self.checkpointer = CatalogCheckpointer(
            self.root / "catalog.sqlite3", self.storage_guard, self.diagnostics
        )
        self.encoding_slot = asyncio.Semaphore(1)
        self.queue = None
        self.tasks: list[asyncio.Task] = []
        self.optional_metadata_workers = set()
        self.jobs: dict[str, asyncio.Task] = {}
        self.storage_error = False
        self._closed = False
        self._closing_task = None
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

    @asynccontextmanager
    async def transaction(self):
        """Keep the catalogue lock until FULL commit/rollback finishes on disk."""
        from camera_media_service.media_files import storage_io

        if not self.lock.locked():
            raise RuntimeError("Catalogue transaction requires the recording lock")
        try:
            yield self.db
            with self.diagnostics.measure("catalog_commit"):
                await storage_io(self.db.commit)
        except BaseException:
            await storage_io(self.db.rollback)
            raise

    def _camera(self, camera: str) -> None:
        if camera not in self.cameras:
            raise ApiError(404, "CAMERA_NOT_FOUND", "Camera is not registered.")

    async def check_media_space(self, reserve: int = 0) -> None:
        """One admission path for legacy clips with or without daily recording."""
        if self.capacity_check is not None:
            await self.capacity_check(reserve)
            return
        reservations = dict(self.capacity_reservations)
        outputs = dict(self.capacity_outputs)

        def inspect():
            self.storage_guard()
            allocated = dict.fromkeys(reservations, 0)
            owners = {p: key for key, paths in outputs.items() for p in paths}
            used = 0
            for path in self.root.rglob("*"):
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                if not path.is_file():
                    continue
                used += info.st_size
                if path in owners and owners[path] in allocated:
                    allocated[owners[path]] += info.st_size
            future = reserve + sum(max(0, n - allocated[k]) for k, n in reservations.items())
            if (
                self.max_storage_bytes is not None and used + future > self.max_storage_bytes
            ) or shutil.disk_usage(self.root).free - future < self.min_free_storage_bytes:
                raise ApiError(503, "INSUFFICIENT_STORAGE", "Media storage budget reached.")

        await asyncio.to_thread(inspect)

    def _invalidate_budget_snapshot(self):
        self._buffer_budget_snapshot = None
        self._budget_generation += 1

    async def _check_buffer_capacity(self, reserve: int) -> None:
        if self.max_storage_bytes is None:
            return
        generation = self._budget_generation
        # Encoders reserve before creating outputs and invalidate on completion.
        # Between scans, retain their *unallocated* promised bytes from the same
        # file observations. Check DB/WAL growth on every write, not on a TTL.
        if (
            self._buffer_budget_snapshot is None
            or time.monotonic() - self._buffer_budget_snapshot[0] > 5
        ):
            reservations = dict(self.capacity_reservations)
            owners = {
                str(path): key for key, paths in self.capacity_outputs.items() for path in paths
            }

            def scan():
                self.storage_guard()
                non_buffer, allocated = 0, dict.fromkeys(reservations, 0)
                # JPEG bytes are already maintained in the frame catalogue.
                # Do not walk/stat that hot directory again on every reservation
                # change; only non-buffer files need a physical snapshot.
                for path, info, category in iter_regular_files(self.root, exclude={"buffer"}):
                    if category is not None:
                        non_buffer += info.st_size
                    key = owners.get(path)
                    if key in allocated:
                        allocated[key] += info.st_size
                return non_buffer, allocated

            non_buffer, allocated = await asyncio.to_thread(scan)
            # Reservations may change while the worker runs; only publish a
            # coherent snapshot. A retry sees the fresh producer promises.
            if generation != self._budget_generation:
                return await self._check_buffer_capacity(reserve)
            self._buffer_budget_snapshot = (time.monotonic(), non_buffer, allocated)
        _, non_buffer, allocated = self._buffer_budget_snapshot

        def database_bytes():
            total = 0
            for path in self.root.iterdir():
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            return total

        database = await asyncio.to_thread(database_bytes)
        if generation != self._budget_generation:
            return await self._check_buffer_capacity(reserve)
        promised = sum(
            max(0, size - allocated.get(key, 0)) for key, size in self.capacity_reservations.items()
        )
        buffered = self.db.execute("SELECT coalesce(sum(size),0) FROM frames").fetchone()[0]
        if non_buffer + database + promised + buffered + reserve + 65536 > self.max_storage_bytes:
            raise StorageCapacityError(
                "Media total storage budget reached", condition="total_budget"
            )

    def _save(self, body: dict) -> None:
        self.db.execute(
            "UPDATE jobs SET status=?,body=? WHERE id=?",
            (body["status"], json.dumps(body), body["clip_id"]),
        )

        if self.thumbnail_change_observer:
            try:
                self.thumbnail_change_observer("clip", body["clip_id"], body["status"])
            except Exception:
                LOG.exception("Optional thumbnail notification failed")

    def notify_thumbnail_ready(self, kind, key, metadata):
        if self.thumbnail_observer:
            try:
                self.thumbnail_observer(kind, key, metadata.get("_file_identity"))
            except Exception:
                LOG.exception("Optional thumbnail notification failed")

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
            async with self.transaction():
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
            async with self.transaction():
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

    @asynccontextmanager
    async def thumbnail_deletion(self, kind, key):
        manager = getattr(self, "thumbnail_manager", None)
        if manager:
            async with manager.deletion(kind, key):
                yield
        else:
            yield

    async def delete_clip(self, clip_id: str) -> None:
        async with self.thumbnail_deletion("clip", clip_id):
            async with self.lock:
                body = self._get(clip_id)
                await self._expire(body)
                if body["status"] in ACTIVE or clip_id in self.thumbnail_pins:
                    raise ApiError(409, "CLIP_IN_PROGRESS", "Recording is still in progress.")
                if body["status"] in ("deleted", "expired"):
                    return
                await self._remove_files(clip_id)
                body.update(status="deleted", deleted_at=iso(time.time()), content_path=None)
                async with self.transaction():
                    self._save(body)

    async def _remove_files(self, clip_id: str) -> None:
        # clip_id comes from the catalog, never directly from a supplied filesystem path.
        def remove():
            self.storage_guard()
            for suffix in (".mp4", ".tmp.mp4", ".ffconcat"):
                (self.clips / f"{clip_id}{suffix}").unlink(missing_ok=True)
            try:
                thumbnail = self.root / "clip-thumbnails" / (clip_id + ".jpg")
                if thumbnail.parent.resolve() != thumbnail.parent:
                    raise OSError("Invalid thumbnail cleanup root")
                thumbnail.unlink(missing_ok=True)
            except OSError:
                LOG.warning("Optional clip thumbnail cleanup deferred")

        await asyncio.to_thread(remove)

    async def _expire(self, body: dict) -> None:
        if body["status"] == "ready" and epoch(body["expires_at"]) <= time.time():
            # Deny access immediately, even when physical cleanup must be retried.
            body.update(status="expired", content_path=None)
            async with self.transaction():
                self._save(body)
                self.db.execute(
                    "INSERT OR IGNORE INTO pending_cleanup VALUES (?)", (body["clip_id"],)
                )
        if body["status"] == "expired":
            await self._cleanup_pending(body["clip_id"])

    async def _cleanup_pending(self, clip_id: str) -> None:
        if clip_id in self.thumbnail_pins:
            return
        if self.db.execute("SELECT 1 FROM pending_cleanup WHERE id=?", (clip_id,)).fetchone():
            try:
                await self._remove_files(clip_id)
                async with self.transaction():
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
        await self.store_frames([frame])

    async def store_frames(self, frames: list[FrameEnvelope]) -> None:
        if not frames:
            return
        from camera_media_service.media_files import storage_io

        await asyncio.to_thread(self.storage_guard)
        reserve = sum(len(frame.jpeg) for frame in frames)
        if self.min_free_storage_bytes:
            free = (await asyncio.to_thread(shutil.disk_usage, self.root)).free
            if free - reserve < self.min_free_storage_bytes:
                raise StorageCapacityError(
                    "Media free-space reserve reached", condition="minimum_free"
                )
        async with self.lock:
            await self._prune(time.time(), reserve=reserve)
            await self._check_buffer_capacity(reserve)
            size = self.db.execute("SELECT coalesce(sum(size),0) FROM frames").fetchone()[0]
            if size + reserve > self.max_buffer_bytes:
                raise StorageCapacityError("Protected frames exhausted the buffer budget")
            paths = []
            try:
                async with self.transaction():
                    for frame in frames:
                        name = f"{uuid.uuid4()}.jpg"
                        path = self.buffer / name
                        paths.append(path)
                        await storage_io(path.write_bytes, frame.jpeg)
                        self.db.execute(
                            "INSERT INTO frames VALUES (?,?,?,?)",
                            (name, frame.camera_id, frame.received_at, len(frame.jpeg)),
                        )
            except BaseException:
                for path in paths:
                    try:
                        referenced = self.db.execute(
                            "SELECT 1 FROM frames WHERE path=?", (path.name,)
                        ).fetchone()
                    except sqlite3.Error:
                        # Leave uncertain files for startup reconciliation.
                        continue
                    if not referenced:
                        await storage_io(path.unlink, missing_ok=True)
                raise

    def pin_frames(
        self, owner_id: str, camera_id: str, start_at: float, end_at: float
    ) -> list[StoredFrame]:
        """Caller holds lock/transaction; leases survive process interruption."""
        rows = self.db.execute(
            "SELECT * FROM frames WHERE camera=? AND timestamp>=? AND timestamp<? "
            "ORDER BY timestamp,path",
            (camera_id, start_at, end_at),
        ).fetchall()
        self.db.executemany(
            "INSERT OR IGNORE INTO frame_pins VALUES(?,?)", ((owner_id, r["path"]) for r in rows)
        )
        return [StoredFrame(r["path"], r["timestamp"], r["size"]) for r in rows]

    def release_frames(self, owner_id: str) -> None:
        self.db.execute("DELETE FROM frame_pins WHERE owner=?", (owner_id,))

    async def _prune(self, now: float, reserve: int = 0) -> None:
        from camera_media_service.media_files import storage_io

        await asyncio.to_thread(self.storage_guard)
        total = self.db.execute("SELECT coalesce(sum(size),0) FROM frames").fetchone()[0]
        cutoff = now - 300 if total + reserve <= self.max_buffer_bytes else float("inf")
        protection = ""
        parameters = [cutoff]
        for camera, cursor in self.daily_frame_watermarks.items():
            protection += " AND NOT (f.camera=? AND f.timestamp>=?)"
            parameters.extend((camera, cursor))
        # Reclaim a small batch during normal ingestion. Under actual byte
        # pressure retain enough candidates to satisfy the existing budget.
        pressured = total + reserve > self.max_buffer_bytes
        parameters.append(-1 if pressured else 64)
        rows = self.db.execute(
            "SELECT * FROM frames f WHERE timestamp < ? "
            + protection
            + """
            AND NOT EXISTS (
                SELECT 1 FROM jobs j WHERE j.camera=f.camera
                AND j.status IN ('collecting','processing')
                AND f.timestamp >= j.start AND f.timestamp < j.end)
            AND NOT EXISTS(SELECT 1 FROM frame_pins p WHERE p.path=f.path)
        """
            + " ORDER BY timestamp,path LIMIT ?",
            parameters,
        ).fetchall()
        removed = []
        try:
            for row in rows:
                if len(removed) >= 64 and total + reserve <= self.max_buffer_bytes:
                    break
                if row["timestamp"] >= now - 300 and total + reserve <= self.max_buffer_bytes:
                    break

                def remove(row=row):
                    self.storage_guard()
                    (self.buffer / row["path"]).unlink(missing_ok=True)
                    # Keep successful unlink results even if cancellation arrives
                    # before the drained worker returns to the event loop.
                    removed.append((row["path"],))

                await storage_io(remove)
                total -= row["size"]
        finally:
            # One FULL-synchronous commit for a backlog, rather than one fsync per old frame.
            # Also retain successful cleanup when a later unlink fails or shutdown cancels it.
            if removed:
                try:
                    self.db.executemany("DELETE FROM frames WHERE path=?", removed)
                    with self.diagnostics.measure("prune_commit"):
                        await storage_io(self.db.commit)
                except BaseException:
                    await storage_io(self.db.rollback)
                    raise

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
                async with self.transaction():
                    self._save(body)
                if not frames:
                    return
            try:
                self.capacity_reservations[clip_id] = (
                    int(sum(r["size"] for r in frames) * 1.2) + 1048576
                )
                self.capacity_outputs[clip_id] = (
                    self.clips / f"{clip_id}.tmp.mp4",
                    self.clips / f"{clip_id}.mp4",
                )
                await self.check_media_space()
                metadata = await encode_clip(
                    self.ffmpeg,
                    self.buffer,
                    self.clips,
                    clip_id,
                    [(r["path"], r["timestamp"]) for r in frames],
                )
                self.capacity_reservations[clip_id] = 0
                await self.check_media_space()
                now = time.time()
                body.update(
                    status="ready",
                    completed_at=iso(now),
                    expires_at=iso(now + body["recording_policy"]["retention_days"] * 86400),
                    content_path=f"/api/v1/clips/{clip_id}/content",
                    size_bytes=(self.clips / f"{clip_id}.mp4").stat().st_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.exception("Clip encoding failed: %s", clip_id)
                body.update(
                    status="failed",
                    error={
                        "code": exc.code
                        if isinstance(exc, ApiError)
                        else "STORAGE_UNAVAILABLE"
                        if isinstance(exc, OSError)
                        else "ENCODING_FAILED",
                        "message": "Clip creation failed.",
                    },
                )
                await self._remove_files(clip_id)
            finally:
                self.capacity_reservations.pop(clip_id, None)
                self.capacity_outputs.pop(clip_id, None)
            if body["status"] == "ready" and metadata:
                await persist_generated_info(self, self.clips / f"{clip_id}.mp4", metadata)
            while True:
                try:
                    async with self.lock:
                        async with self.transaction():
                            self._save(body)
                    break
                except sqlite3.Error:
                    LOG.exception("Retrying clip result persistence: %s", clip_id)
                    await asyncio.sleep(1)
            if body["status"] == "ready" and metadata:
                self.notify_thumbnail_ready("clip", clip_id, metadata)

    async def start(self) -> None:
        await self.checkpointer.start()
        # Absorb measured SD commit stalls without allowing unbounded RAM growth.
        # The writer owns at most ten additional validated frames outside this queue.
        self.queue = self.hub.subscribe(maxsize=1200, recording=True, max_bytes=96 * 1024**2)
        assert isinstance(self.queue, RecordingQueue)
        self.tasks = [
            asyncio.create_task(self._writer()),
            asyncio.create_task(self._scheduler()),
            asyncio.create_task(self._observe_loop()),
        ]

    async def _observe_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            expected = loop.time() + 0.25
            wall_start = time.time() + 0.25
            await asyncio.sleep(0.25)
            delayed = max(0, loop.time() - expected)
            self.diagnostics.observe("event_loop_lag", delayed)
            if delayed > 1:
                LOG.warning(
                    "Recording event loop delayed: seconds=%.3f expected_at=%s observed_at=%s",
                    delayed,
                    iso(wall_start),
                    iso(time.time()),
                )

    async def _writer(self) -> None:
        assert self.queue is not None
        while True:
            frame = await self.queue.get()
            frames = [frame]
            try:
                deadline = asyncio.get_running_loop().time() + 0.1
                while len(frames) < 10:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        frames.append(await asyncio.wait_for(self.queue.get(), remaining))
                    except TimeoutError:
                        break
                with self.diagnostics.measure("frame_store"):
                    await self.store_frames(frames)
                self.storage_error = False
            except (OSError, sqlite3.Error) as exc:
                self.storage_error = True
                self.diagnostics.count("frame_store_" + getattr(exc, "condition", "io_error"))
                LOG.exception("Recording frame storage failed")
                if self.hub.loss_observer is not None:
                    for frame in frames:
                        self.hub.loss_observer(
                            frame.camera_id,
                            frame.received_at,
                            frame.received_at + 1 / 30,
                            "STORAGE_FULL"
                            if isinstance(exc, StorageCapacityError)
                            else "STORAGE_ERROR",
                        )
            finally:
                for frame in frames:
                    self.queue.acknowledge(frame)
                self.diagnostics.gauge("ingest_queue_frames", self.queue.qsize())
                self.diagnostics.gauge("ingest_queue_bytes", self.queue.queued_bytes)
                self.diagnostics.report_if_due()

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
        from camera_media_service.media_files import _settle_worker

        if self._closing_task is None:
            self._closed = True
            self._closing_task = asyncio.create_task(self._close_resources())
        try:
            await asyncio.shield(self._closing_task)
        except asyncio.CancelledError:
            await _settle_worker(self._closing_task)
            raise

    async def _close_resources(self) -> None:
        optional_workers = list(self.optional_metadata_workers)
        for optional in optional_workers:
            optional.stop()
        if self.queue is not None:
            self.hub.unsubscribe(self.queue)
        # Stop producers/maintenance first, then drain already accepted receipts.
        writer = self.tasks[0] if self.tasks else None
        tasks = [*self.tasks[1:], *self.jobs.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer_failed = False
        if self.queue is not None and writer is not None:
            drain = asyncio.create_task(self.queue.join())
            await asyncio.wait((drain, writer), return_when=asyncio.FIRST_COMPLETED)
            writer_failed = writer.done()
            if writer_failed:
                LOG.error("Recording writer stopped before shutdown drain completed")
                while not self.queue.empty():
                    frame = self.queue.get_nowait()
                    self.queue.acknowledge(frame)
                    self.diagnostics.count("shutdown_writer_unavailable")
                    if self.hub.loss_observer is not None:
                        self.hub.loss_observer(
                            frame.camera_id,
                            frame.received_at,
                            frame.received_at + 1 / 30,
                            "STORAGE_ERROR",
                        )
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await asyncio.gather(*(optional.close() for optional in optional_workers))
        await self.checkpointer.close()
        close_probe_database(self.db)
        from camera_media_service.media_files import storage_io

        await storage_io(self.db.close)
        if writer_failed:
            raise RuntimeError("Recording writer stopped before shutdown drain completed")
