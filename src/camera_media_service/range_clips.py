"""Admission and explicit retry for request-specific recorded time ranges."""

from __future__ import annotations

import asyncio
import json
import uuid

from camera_media_service.recorded_clips import event_timestamp
from camera_media_service.recording import ACTIVE, ApiError, iso

MAX_RANGE_SECONDS = 3600


def duplicate(body, start, end):
    if (
        body.get("request_mode") != "time_range"
        or body["requested_start_at"] != iso(start)
        or body["requested_end_at"] != iso(end)
    ):
        raise ApiError(
            409, "IDEMPOTENCY_CONFLICT", "event_id already identifies a different request."
        )


async def create_range(service, camera, event, start_at, end_at, *, retry_failed=False):
    if (
        not isinstance(camera, str)
        or not isinstance(event, str)
        or not event.strip()
        or len(event) > 128
    ):
        raise ApiError(400, "INVALID_ARGUMENT", "Invalid camera_id or event_id.")
    if type(retry_failed) is not bool:
        raise ApiError(400, "INVALID_ARGUMENT", "retry_failed must be boolean.")
    start, end = event_timestamp(start_at), event_timestamp(end_at)
    if not 0 < end - start <= MAX_RANGE_SECONDS:
        raise ApiError(
            400, "INVALID_ARGUMENT", "Requested range must be positive and at most 3600 seconds."
        )
    recording, daily = service.recording, service.daily
    recording._camera(camera)
    old = None
    async with service.lock:
        row = service.db.execute(
            "SELECT body FROM jobs WHERE camera=? AND event=?", (camera, event)
        ).fetchone()
        if row:
            old = json.loads(row[0])
            duplicate(old, start, end)
            await recording._expire(old)
            if old["status"] != "failed" or not retry_failed:
                return old, False
    # A previous failed attempt may still be releasing files and source leases.
    # Never let its finalizer remove the next attempt's resources.
    previous = service.jobs.get(old["clip_id"]) if old else None
    if previous is not None:
        await asyncio.shield(previous)
    now = int(daily.clock() * 1000) / 1000
    if start >= now:
        raise ApiError(400, "REQUEST_TIME_IN_FUTURE", "The range must start before now.")
    key = old["clip_id"] if old else str(uuid.uuid4())
    admitted = False
    pinned = False
    try:
        async with service.lock:
            if service.closed:
                raise ApiError(503, "SERVICE_STOPPING", "Media service is stopping.")
            if old and recording._get(key)["status"] != "failed":
                raise ApiError(
                    409, "RETRY_NOT_ALLOWED", "Only a failed time-range request can be retried."
                )
            if (
                service.db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN (?,?)", ACTIVE
                ).fetchone()[0]
                >= recording.max_jobs
            ):
                raise ApiError(503, "SERVICE_BUSY", "Too many active recordings.")
            slices, gaps = service._select(camera, None, now, start, end)
            policy = old["recording_policy"] if old else recording._policy(camera)
            reserve = int(sum(s["size_bytes"] for s in slices) * 1.2) + 1048576
            with service.db:
                service.db.executemany(
                    "INSERT OR IGNORE INTO clip_recording_pins VALUES(?,?)",
                    ((key, s["recording_id"]) for s in slices),
                )
            pinned = True
            daily.storage_reservations[key] = reserve
        # This scans files; it must not hold the ingest/catalog lock.
        await daily.check_space()
        async with service.lock:
            if service.closed:
                raise ApiError(503, "SERVICE_STOPPING", "Media service is stopping.")
            # Legacy/event-time callers do not share range_admission and may
            # have claimed the same key while the filesystem scan was unlocked.
            current = service.db.execute(
                "SELECT body FROM jobs WHERE camera=? AND event=?", (camera, event)
            ).fetchone()
            if old is None and current is not None:
                existing = json.loads(current[0])
                duplicate(existing, start, end)
                return existing, False
            if old and recording._get(key)["status"] != "failed":
                raise ApiError(
                    409, "RETRY_NOT_ALLOWED", "Request was deleted while retry was being admitted."
                )
            if (
                service.db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN (?,?)", ACTIVE
                ).fetchone()[0]
                >= recording.max_jobs
            ):
                raise ApiError(503, "SERVICE_BUSY", "Too many active recordings.")
            body = dict(
                clip_id=key,
                camera_id=camera,
                event_id=event,
                request_mode="time_range",
                status="processing",
                coverage="partial" if gaps else "complete",
                accepted_at=old["accepted_at"] if old else iso(now),
                attempt_started_at=iso(now),
                attempt_count=old["attempt_count"] + 1 if old else 1,
                requested_start_at=iso(start),
                requested_end_at=iso(end),
                recording_policy=policy,
                actual_start_at=iso(slices[0]["clip_start"]),
                actual_end_at=iso(slices[-1]["clip_end"]),
                gaps=gaps,
                completed_at=None,
                expires_at=None,
                deleted_at=None,
                content_path=None,
                error=None,
                extraction_mode="stream_copy",
                size_bytes=None,
                media_info=None,
                available_intervals=[
                    {"start_at": iso(s["clip_start"]), "end_at": iso(s["clip_end"])} for s in slices
                ],
            )
            with service.db:
                if old:
                    recording._save(body)
                else:
                    service.db.execute(
                        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                        (key, camera, event, now, start, end, "processing", json.dumps(body)),
                    )
                service.db.execute(
                    "INSERT OR REPLACE INTO clip_sources VALUES(?,?)", (key, json.dumps(slices))
                )
            task = asyncio.create_task(service._process(key, slices))
            service.jobs[key] = task
            task.add_done_callback(lambda done: service._done(key, done))
            admitted = True
            return body, True
    finally:
        if pinned and not admitted:
            async with service.lock:
                with service.db:
                    service.db.execute("DELETE FROM clip_recording_pins WHERE clip_id=?", (key,))
                daily.storage_reservations.pop(key, None)
