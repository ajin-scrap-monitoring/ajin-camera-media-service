"""Optional, bounded and identity-bound thumbnails for existing ready MP4s."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

from camera_media_service.media_files import open_media_file, storage_io
from camera_media_service.persisted_media_info import file_identity
from camera_media_service.recording import ApiError, epoch, iso
from camera_media_service.thumbnail_media import (
    MAX_JPEG_BYTES,
    ThumbnailFailure,
    generate_thumbnail,
)
from camera_media_service.thumbnail_store import ThumbnailStore

LOG = logging.getLogger(__name__)
FIELDS = (
    "status",
    "representative_at",
    "time_basis",
    "content_path",
    "width",
    "height",
    "size_bytes",
    "reason_code",
    "generated_at",
)


async def settle(coroutine):
    task = asyncio.ensure_future(coroutine)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class ThumbnailService:
    def __init__(
        self,
        recording,
        daily=None,
        exports=None,
        *,
        enabled=True,
        max_jobs=4,
        timeout=15,
        max_bytes=MAX_JPEG_BYTES,
    ):
        self.recording, self.daily, self.exports = recording, daily, exports
        self.enabled, self.max_jobs, self.timeout, self.max_bytes = (
            enabled,
            max_jobs,
            timeout,
            max_bytes,
        )
        self.ffmpeg = recording.ffmpeg
        self.clock = daily.clock if daily else time.time
        self.store = ThumbnailStore(recording.root)
        self.slot = asyncio.Semaphore(1)
        self.admission = asyncio.Lock()
        self.jobs = {}
        self.admitting = set()
        self.notifications = {}
        self.unreleased = set()
        self.available = False
        self.closed = False
        self.loop_task = None
        self.page_after = None
        recording.thumbnail_pins = getattr(recording, "thumbnail_pins", set())

    @staticmethod
    def validate(kind, key):
        try:
            if kind not in ("clip", "day") or str(uuid.UUID(key)) != key:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError(400, "INVALID_ARGUMENT", "Use a canonical recording UUID.") from exc

    def _path(self, kind, key):
        self.validate(kind, key)
        return (
            self.recording.root
            / ("clip-thumbnails" if kind == "clip" else "daily-thumbnails")
            / (key + ".jpg")
        )

    def _base(self, kind, key, camera, status, reason=None):
        return {
            ("clip_id" if kind == "clip" else "recording_id"): key,
            "camera_id": camera,
            "status": status,
            "representative_at": None,
            "time_basis": None,
            "content_path": None,
            "width": None,
            "height": None,
            "size_bytes": None,
            "reason_code": reason,
            "generated_at": None,
        }

    async def start(self):
        if not self.enabled:
            return
        try:
            await self.recording.check_media_space(65536)

            def folders():
                self.recording.storage_guard()
                for name in ("clip-thumbnails", "daily-thumbnails"):
                    folder = self.recording.root / name
                    if folder.resolve() != folder:
                        raise OSError("Invalid thumbnail root")
                    folder.mkdir(exist_ok=True)
                    for path in folder.glob("*.tmp.jpg"):
                        parts = path.name.split(".")
                        if len(parts) == 4 and parts[-2:] == ["tmp", "jpg"]:
                            try:
                                uuid.UUID(parts[0])
                                uuid.UUID(parts[1])
                            except ValueError:
                                continue
                            path.unlink(missing_ok=True)

            await storage_io(folders)
            await self.store.start()
            async with self.recording.lock:
                async with self.recording.transaction():
                    self.recording.db.execute(
                        "DELETE FROM clip_recording_pins WHERE clip_id LIKE 'thumbnail:%'"
                    ) if self.daily else None
            for kind, key in await self.store.pending():
                row = await self.store.get(kind, key)
                await self._purge(kind, key)
                await self.store.put(
                    kind, key, self._base(kind, key, row["camera_id"], "failed", "JOB_INTERRUPTED")
                )
            self.available = True
            self.recording.thumbnail_manager = self
            self.recording.thumbnail_observer = self.notify_ready
            self.recording.thumbnail_change_observer = self.notify_change
            if self.daily:
                self.daily.catalog.thumbnail_change_observer = self.notify_change
            self.loop_task = asyncio.create_task(self._maintenance())
        except (OSError, sqlite3.Error, ApiError):
            LOG.exception("Optional thumbnail storage could not start")

    def notify_ready(self, kind, key, identity):
        if (
            self.available
            and not self.closed
            and (len(self.notifications) < 64 or (kind, key) in self.notifications)
        ):
            self.notifications[kind, key] = ("ready", identity)

    def notify_change(self, kind, key, status):
        if status in ("deleted", "expired") and self.available and not self.closed:
            if len(self.notifications) < 64 or (kind, key) in self.notifications:
                self.notifications[kind, key] = (status, None)

    async def _parent(self, kind, key):
        # Caller holds the video catalogue lock. This checks access before all caches.
        if kind == "clip":
            body = self.recording._get(key)
            self.recording._camera(body["camera_id"])
            await self.recording._expire(body)
            if body["status"] in ("deleted", "expired"):
                raise ApiError(
                    410, "CLIP_" + body["status"].upper(), "Clip is no longer available."
                )
            path = self.recording.clips / (key + ".mp4")
            reason = None if body["status"] == "ready" else "SOURCE_NOT_READY"
        else:
            if not self.daily:
                raise ApiError(404, "DAILY_RECORDING_NOT_FOUND", "Daily recording unavailable.")
            body = self.daily.catalog.get_day(key)
            self.recording._camera(body["camera_id"])
            self.exports._accessible(body)
            path = self.recording.root / "daily-exports" / (key + ".mp4")
            reason = (
                None
                if body["status"] == "closed"
                and body["export_state"] == "ready"
                and body.get("archive_state") == "mp4"
                else "DAILY_MP4_REQUIRED"
            )
        return body, path, reason

    async def _identity(self, path, folder):
        def inspect():
            self.recording.storage_guard()
            if folder.resolve() != folder or path.is_symlink() or path.resolve().parent != folder:
                raise OSError("Invalid owned media path")
            return file_identity(path.stat())

        return await storage_io(inspect)

    async def _snapshot(self, kind, key):
        self.validate(kind, key)
        async with self.recording.lock:
            body, path, reason = await self._parent(kind, key)
            identity = None
            if reason is None:
                try:
                    identity = await self._identity(path, path.parent)
                except FileNotFoundError:
                    reason = "SOURCE_UNAVAILABLE"
        return body, path, identity, reason

    def _ensure(self):
        if self.closed or not self.available:
            raise ApiError(503, "THUMBNAIL_STORAGE_UNAVAILABLE", "Thumbnail storage unavailable.")

    async def _purge(self, kind, key):
        def remove():
            self.recording.storage_guard()
            path = self._path(kind, key)
            if path.parent.resolve() != path.parent:
                raise OSError("Invalid thumbnail cleanup root")
            path.unlink(missing_ok=True)

        await storage_io(remove)
        self.recording._invalidate_budget_snapshot()

    async def info(self, kind, key, *, _attempt=0):
        if self.closed:
            raise ApiError(503, "THUMBNAIL_STORAGE_UNAVAILABLE", "Thumbnail service closed.")
        body, path, identity, reason = await self._snapshot(kind, key)
        if not self.enabled:
            return self._base(kind, key, body["camera_id"], "unsupported")
        self._ensure()
        row = await self.store.get(kind, key)
        if reason:
            return self._base(kind, key, body["camera_id"], "unavailable", reason)
        if row is None:
            return self._base(kind, key, body["camera_id"], "unavailable", "NOT_PREPARED")
        token = (kind, key)
        task = self.jobs.get(token)
        if (
            row["status"] == "pending"
            and token not in self.admitting
            and (task is None or task.done())
        ):
            async with self.recording.lock:
                if await self.store.get(kind, key) == row and token not in self.admitting:
                    repaired = self._base(kind, key, body["camera_id"], "failed", "JOB_INTERRUPTED")
                    repaired["_source_identity"] = row.get("_source_identity")
                    repaired["_representative_at"] = row.get("_representative_at")
                    await self.store.put(kind, key, repaired)
                    row = repaired
        invalid = None
        if tuple(row.get("_source_identity") or ()) != identity:
            invalid = "SOURCE_CHANGED"
        elif row["status"] == "ready":
            try:
                image_identity = await self._identity(
                    self._path(kind, key), self._path(kind, key).parent
                )
                if tuple(row.get("_image_identity", ())) != image_identity:
                    invalid = "IMAGE_CHANGED"
            except FileNotFoundError:
                invalid = "IMAGE_UNAVAILABLE"
        if invalid:
            retry = False
            async with self.recording.lock:
                # Publication takes this same lock. A stale GET may invalidate only
                # the exact row/source snapshot it observed, never a newer image.
                current_body, current_path, current_reason = await self._parent(kind, key)
                current_identity = (
                    None
                    if current_reason
                    else await self._identity(current_path, current_path.parent)
                )
                if await self.store.get(kind, key) != row or current_identity != identity:
                    retry = True
                else:
                    await self._purge(kind, key)
                    row = self._base(kind, key, current_body["camera_id"], "unavailable", invalid)
                    row["_source_identity"] = identity
                    await self.store.put(kind, key, row)
            if retry:
                if _attempt < 2:
                    return await self.info(kind, key, _attempt=_attempt + 1)
                return self._base(kind, key, body["camera_id"], "unavailable", "SOURCE_CHANGED")
        return {
            ("clip_id" if kind == "clip" else "recording_id"): key,
            "camera_id": body["camera_id"],
            **{field: row.get(field) for field in FIELDS},
        }

    @staticmethod
    def _representative(body):
        parts = body.get("available_intervals") or []
        if parts:
            first = parts[0]
            try:
                if first["playback_start_seconds"] == 0 and epoch(first["end_at"]) > epoch(
                    first["start_at"]
                ):
                    return first["start_at"]
            except (ValueError, KeyError, TypeError):
                pass
        try:
            if epoch(body["actual_end_at"]) > epoch(body["actual_start_at"]):
                return body["actual_start_at"]
        except (KeyError, TypeError, ValueError):
            pass
        return None

    async def prepare(self, kind, key, *, retry_failed=False, attested_identity=None):
        if type(retry_failed) is not bool:
            raise ApiError(400, "INVALID_ARGUMENT", "retry_failed must be boolean.")
        async with self.admission:
            current = await self.info(kind, key)
            if not self.enabled or current["status"] in ("ready", "pending"):
                return current, False
            if current["reason_code"] in (
                "SOURCE_NOT_READY",
                "SOURCE_UNAVAILABLE",
                "DAILY_MP4_REQUIRED",
            ):
                return current, False
            if current["status"] == "failed" and not retry_failed:
                return current, False
            token = (kind, key)
            if token in self.jobs and not self.jobs[token].done():
                return current, False
            self._ensure()
            if sum(not task.done() for task in self.jobs.values()) >= self.max_jobs:
                raise ApiError(503, "THUMBNAIL_CAPACITY_EXCEEDED", "Thumbnail queue is full.")
            body, source, identity, reason = await self._snapshot(kind, key)
            if reason:
                return self._base(kind, key, body["camera_id"], "unavailable", reason), False
            owner = "thumbnail:" + kind + ":" + key
            output = self._path(kind, key)
            temporary = output.with_name(key + "." + str(uuid.uuid4()) + ".tmp.jpg")
            reserve = self.max_bytes + 65536
            pending = self._base(kind, key, body["camera_id"], "pending")
            pending["_source_identity"] = identity
            if attested_identity and tuple(attested_identity) == identity:
                pending["_representative_at"] = self._representative(body)
            else:
                prior = await self.store.get(kind, key)
                if prior and tuple(prior.get("_source_identity") or ()) == identity:
                    pending["_representative_at"] = prior.get("_representative_at")
            self.recording.capacity_reservations[owner] = reserve
            self.recording.capacity_outputs[owner] = (temporary, output)
            self.admitting.add(token)
            try:
                await self.recording.check_media_space()
                await self.store.put(kind, key, pending)
                task = asyncio.create_task(
                    self._run(kind, key, source, identity, temporary, owner, pending)
                )
                self.jobs[token] = task

                def done(finished):
                    if self.jobs.get(token) is finished:
                        self.jobs.pop(token, None)
                    if not finished.cancelled() and finished.exception():
                        LOG.error("Thumbnail worker failed", exc_info=finished.exception())

                task.add_done_callback(done)
            except BaseException:
                self.recording.capacity_reservations.pop(owner, None)
                self.recording.capacity_outputs.pop(owner, None)

                async def abandon():
                    row = await self.store.get(kind, key)
                    if row and row["status"] == "pending":
                        await self.store.put(
                            kind,
                            key,
                            self._base(kind, key, body["camera_id"], "failed", "JOB_INTERRUPTED"),
                        )

                await settle(abandon())
                raise
            finally:
                self.admitting.discard(token)
            return {k: v for k, v in pending.items() if not k.startswith("_")}, True

    async def _release(self, kind, key, owner):
        try:
            async with self.recording.lock:
                if kind == "clip":
                    self.recording.thumbnail_pins.discard(key)
                if kind == "day":
                    async with self.recording.transaction():
                        self.recording.db.execute(
                            "DELETE FROM clip_recording_pins WHERE clip_id=?", (owner,)
                        )
            self.unreleased.discard((kind, key, owner))
        except (OSError, sqlite3.Error):
            if len(self.unreleased) < 128:
                self.unreleased.add((kind, key, owner))
            LOG.exception("Thumbnail source lease cleanup will retry")
        finally:
            self.recording.capacity_reservations.pop(owner, None)
            self.recording.capacity_outputs.pop(owner, None)

    @asynccontextmanager
    async def deletion(self, kind, key):
        # Exclude re-admission until the caller commits the parent's tombstone.
        # Workers do not take admission while draining, so lock order stays
        # admission -> video catalogue and child cleanup can always finish.
        async with self.admission:
            await self.cancel_for_delete(kind, key)
            yield

    async def cancel_for_delete(self, kind, key):
        task = self.jobs.get((kind, key))
        if task is None:
            return
        if not task.done():
            task.cancel()
        await settle(asyncio.gather(task, return_exceptions=True))
        await settle(self._release(kind, key, "thumbnail:" + kind + ":" + key))
        row = await self.store.get(kind, key)
        if row and row["status"] == "pending":
            await self.store.put(
                kind, key, self._base(kind, key, row["camera_id"], "failed", "JOB_INTERRUPTED")
            )

    async def _run(self, kind, key, source, identity, temporary, owner, pending):
        error = None
        cancelled = False
        published = False
        try:
            async with self.slot:
                async with self.recording.lock:
                    body, path, reason = await self._parent(kind, key)
                    if reason or await self._identity(path, path.parent) != identity:
                        raise ThumbnailFailure("SOURCE_CHANGED")
                    if kind == "clip":
                        self.recording.thumbnail_pins.add(key)
                    else:
                        async with self.recording.transaction():
                            self.recording.db.execute(
                                "INSERT INTO clip_recording_pins VALUES(?,?)", (owner, key)
                            )
                measured = await generate_thumbnail(
                    self.ffmpeg, source, temporary, timeout=self.timeout, max_bytes=self.max_bytes
                )
                self.recording.capacity_reservations[owner] = 0
                await self.recording.check_media_space()
                async with self.recording.lock:
                    body, path, reason = await self._parent(kind, key)
                    if reason or await self._identity(path, path.parent) != identity:
                        raise ThumbnailFailure("SOURCE_CHANGED")
                    output = self._path(kind, key)
                    await storage_io(os.replace, temporary, output)
                    image_identity = await self._identity(output, output.parent)
                    ready = self._base(kind, key, body["camera_id"], "ready")
                    representative = pending.get("_representative_at")
                    ready.update(
                        measured,
                        representative_at=representative,
                        time_basis="server_received_at" if representative else None,
                        reason_code=None if representative else "TIMELINE_UNAVAILABLE",
                        generated_at=iso(self.clock()),
                        content_path=(
                            f"/api/v1/clips/{key}/thumbnail"
                            if kind == "clip"
                            else f"/api/v2/daily-recordings/{key}/thumbnail"
                        ),
                        _source_identity=identity,
                        _image_identity=image_identity,
                    )
                    await self.store.put(kind, key, ready)
                    published = True
        except asyncio.CancelledError:
            cancelled = True
            error = "JOB_INTERRUPTED"
        except TimeoutError:
            error = "GENERATION_TIMEOUT"
        except ThumbnailFailure as exc:
            error = exc.reason
        except ApiError as exc:
            error = exc.code if exc.status not in (404, 410) else "SOURCE_UNAVAILABLE"
        except (OSError, sqlite3.Error):
            LOG.exception("Optional thumbnail generation failed")
            error = "STORAGE_UNAVAILABLE"
        except Exception:
            LOG.exception("Optional thumbnail generation failed")
            error = "GENERATION_FAILED"
        finally:

            async def cleanup():
                try:
                    try:
                        await storage_io(temporary.unlink, missing_ok=True)
                    except OSError:
                        LOG.exception("Optional thumbnail temporary cleanup deferred")
                    if not published:
                        try:
                            await self._purge(kind, key)
                        except OSError:
                            LOG.exception("Optional thumbnail image cleanup deferred")
                        failed = self._base(
                            kind,
                            key,
                            pending["camera_id"],
                            "unavailable"
                            if error in ("NO_VIDEO_FRAME", "SOURCE_CHANGED", "SOURCE_UNAVAILABLE")
                            else "failed",
                            error or "GENERATION_FAILED",
                        )
                        failed["_source_identity"] = identity
                        failed["_representative_at"] = pending.get("_representative_at")
                        await self.store.put(kind, key, failed)
                finally:
                    await self._release(kind, key, owner)

            await settle(cleanup())
        if cancelled:
            raise asyncio.CancelledError

    async def open_image(self, kind, key):
        info = await self.info(kind, key)
        if info["status"] != "ready":
            status = (
                409 if info["status"] == "pending" else 503 if info["status"] == "failed" else 404
            )
            raise ApiError(
                status,
                "THUMBNAIL_NOT_READY" if status == 409 else "THUMBNAIL_UNAVAILABLE",
                "Thumbnail unavailable.",
            )
        async with self.recording.lock:
            body, source, reason = await self._parent(kind, key)
            row = await self.store.get(kind, key)
            if (
                not row
                or reason
                or tuple(row.get("_source_identity") or ())
                != await self._identity(source, source.parent)
            ):
                raise ApiError(404, "THUMBNAIL_UNAVAILABLE", "Thumbnail unavailable.")
            path = self._path(kind, key)
            file, size = await open_media_file(
                path, root=path.parent, guard=self.recording.storage_guard
            )
            try:
                actual = file_identity(await storage_io(os.fstat, file.fileno()))
                if (
                    row["status"] != "ready"
                    # Windows path/handle ctime APIs differ after rename. Bind the
                    # opened inode/bytes and separately recheck the full path identity.
                    or actual[:4] != tuple(row.get("_image_identity", ()))[:4]
                    or await self._identity(path, path.parent)
                    != tuple(row.get("_image_identity", ()))
                    or size > self.max_bytes
                ):
                    raise ApiError(404, "THUMBNAIL_UNAVAILABLE", "Thumbnail unavailable.")
            except BaseException:
                await storage_io(file.close)
                raise
            return file, size

    async def _maintenance(self):
        while not self.closed:
            try:
                await self._maintenance_loop()
            except (OSError, sqlite3.Error):
                LOG.exception("Optional thumbnail maintenance will retry")
                await asyncio.sleep(5)

    async def _maintenance_loop(self):
        try:
            tick = 0
            while not self.closed:
                await asyncio.sleep(0.25)
                for kind, key in list(self.notifications)[:4]:
                    status, identity = self.notifications.pop((kind, key))
                    try:
                        if status == "ready":
                            await self.prepare(kind, key, attested_identity=identity)
                        else:
                            try:
                                await self._snapshot(kind, key)
                            except ApiError as gone:
                                if gone.status in (404, 410):
                                    await self._purge(kind, key)
                    except ApiError as exc:
                        if exc.code == "THUMBNAIL_CAPACITY_EXCEEDED":
                            try:
                                body, _, identity, _ = await self._snapshot(kind, key)
                                failed = self._base(
                                    kind, key, body["camera_id"], "failed", "CAPACITY_EXCEEDED"
                                )
                                failed["_source_identity"] = identity
                                await self.store.put(kind, key, failed)
                            except ApiError:
                                pass
                    except (OSError, sqlite3.Error):
                        LOG.exception("Thumbnail notification failed")
                tick += 1
                if tick % 20:
                    continue
                for kind, key, owner in list(self.unreleased)[:4]:
                    await self._release(kind, key, owner)
                page = await self.store.page(self.page_after)
                self.page_after = page[-1] if page else None
                for kind, key in page:
                    try:
                        body, _, _, reason = await self._snapshot(kind, key)
                    except ApiError as exc:
                        if exc.status not in (404, 410):
                            continue
                        try:
                            await self._purge(kind, key)
                        except OSError:
                            LOG.exception("Thumbnail cleanup will retry")
        except asyncio.CancelledError:
            raise

    async def close(self):
        async with self.admission:
            if self.closed:
                return
            self.closed = True
        if getattr(self.recording, "thumbnail_manager", None) is self:
            self.recording.thumbnail_manager = None
        if getattr(self.recording, "thumbnail_observer", None) == self.notify_ready:
            self.recording.thumbnail_observer = None
        if getattr(self.recording, "thumbnail_change_observer", None) == self.notify_change:
            self.recording.thumbnail_change_observer = None
        if (
            self.daily
            and getattr(self.daily.catalog, "thumbnail_change_observer", None) == self.notify_change
        ):
            self.daily.catalog.thumbnail_change_observer = None
        if self.loop_task:
            self.loop_task.cancel()
        tasks = list(self.jobs.items())
        for _, task in tasks:
            task.cancel()
        await settle(
            asyncio.gather(
                *[t for _, t in tasks],
                *([self.loop_task] if self.loop_task else []),
                return_exceptions=True,
            )
        )
        for (kind, key), _ in tasks:
            owner = "thumbnail:" + kind + ":" + key
            await settle(self._release(kind, key, owner))
            if self.available:
                row = await self.store.get(kind, key)
                if row and row["status"] == "pending":
                    await self.store.put(
                        kind,
                        key,
                        self._base(kind, key, row["camera_id"], "failed", "JOB_INTERRUPTED"),
                    )
        await self.store.close()
