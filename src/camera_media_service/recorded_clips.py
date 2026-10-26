"""Freeze event-time windows in existing recordings and remux them asynchronously."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid

from camera_media_service.recording import ACTIVE, ApiError, epoch, iso

LOG = logging.getLogger(__name__)


def event_timestamp(value: object) -> float:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value
    ):
        raise ApiError(400, "INVALID_ARGUMENT", "event_at must be UTC with milliseconds and Z.")
    try:
        return epoch(value)
    except (ValueError, OverflowError, OSError) as exc:
        raise ApiError(400, "INVALID_ARGUMENT", "Invalid event_at.") from exc


class RecordedClipService:
    def __init__(self, recording, daily):
        self.recording, self.daily = recording, daily
        self.db, self.lock = recording.db, recording.lock
        self.jobs = {}
        self.closed = False
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS clip_sources(clip_id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clip_recording_pins(
                clip_id TEXT NOT NULL,day_id TEXT NOT NULL,PRIMARY KEY(clip_id,day_id));
        """)
        # RecordingService already marks interrupted jobs failed on restart.
        with self.db:
            self.db.execute(
                "DELETE FROM clip_recording_pins WHERE clip_id NOT IN "
                "(SELECT id FROM jobs WHERE status IN ('collecting','processing'))"
            )
            self.db.execute(
                "DELETE FROM clip_sources WHERE clip_id NOT IN "
                "(SELECT id FROM jobs WHERE status IN ('collecting','processing'))"
            )

    def _select(self, camera, at, now, start, end):
        rows = self.db.execute(
            "SELECT s.body,s.status AS segment_status,d.body AS day FROM daily_segments s "
            "JOIN daily_recordings d ON d.id=s.day_id "
            "WHERE d.camera=? AND d.status NOT IN ('deleted','expired') "
            "AND s.start<? AND s.end>? ORDER BY s.start,s.id",
            (camera, max(min(end, now), at + 0.001), min(start, at)),
        ).fetchall()
        segments = []
        pending = False
        finalized_center = False
        for row in rows:
            item, day = json.loads(row["body"]), json.loads(row["day"])
            if item["start_at"] <= at < item["end_at"] and row["segment_status"] != "processing":
                finalized_center = True
            if day["expires_at"] and epoch(day["expires_at"]) <= now:
                continue
            if row["segment_status"] == "ready":
                path = (self.recording.root / item["path"]).resolve()
                if (
                    path.is_relative_to((self.recording.root / "daily").resolve())
                    and path.is_file()
                ):
                    segments.append(item)
            elif row["segment_status"] == "processing" and item["start_at"] <= at < item["end_at"]:
                pending = True
        losses = [
            (r["start"], r["end"], r["reason"])
            for r in self.db.execute(
                "SELECT start,end,reason FROM daily_gaps WHERE camera=? AND start<? AND end>? "
                "ORDER BY start,end",
                (camera, max(end, at + 0.001), min(start, at)),
            )
        ]
        losses.extend((a, b, reason) for cam, a, b, reason in self.daily.losses if cam == camera)
        offline = self.daily.offline.get(camera)
        if offline is not None and offline < now:
            losses.append((offline, now, "CAMERA_DISCONNECTED"))
        center_lost = any(a <= at < b for a, b, _ in losses)
        if not any(s["start_at"] <= at < s["end_at"] for s in segments) or center_lost:
            if not center_lost and (
                pending or (not finalized_center and self._pending_input(camera, at))
            ):
                raise ApiError(409, "RECORDING_NOT_READY", "Recording is still being prepared.")
            raise ApiError(404, "RECORDING_NOT_FOUND_AT_TIME", "No recording at event_at.")

        slices = []
        for item in segments:
            spans = [(max(start, item["start_at"]), min(end, now, item["end_at"]))]
            for a, b, _ in losses:
                next_spans = []
                for left, right in spans:
                    if a >= right or b <= left:
                        next_spans.append((left, right))
                    else:
                        next_spans.extend(((left, min(a, right)), (max(left, b), right)))
                spans = next_spans
            for left, right in spans:
                if slices:
                    left = max(left, slices[-1]["clip_end"])
                if right > left:
                    min_start = max([item["start_at"], *[b for _, b, _ in losses if b <= left]])
                    max_end = min([item["end_at"], now, *[a for a, _, _ in losses if a >= right]])
                    slices.append(
                        {
                            **item,
                            "clip_start": left,
                            "clip_end": right,
                            "min_start": min_start,
                            "max_end": max_end,
                        }
                    )
        if not slices:
            raise ApiError(404, "RECORDING_NOT_FOUND_AT_TIME", "Requested window has no media.")
        gaps, cursor = [], start
        for left, right in [(s["clip_start"], s["clip_end"]) for s in slices] + [(end, end)]:
            if left - cursor > 0.001:
                # Split a missing tail at 'now' to distinguish absent media from future time.
                edges = sorted({cursor, left, *([now] if cursor < now < left else [])})
                for a, b in zip(edges, edges[1:], strict=False):
                    reason = "FUTURE_NOT_RECORDED" if a >= now else "RECORDING_UNAVAILABLE"
                    for x, y, code in losses:
                        if x <= a and y >= b:
                            reason = code
                            break
                    gaps.append({"start_at": iso(a), "end_at": iso(b), "reason_code": reason})
            cursor = max(cursor, right)
        return slices, gaps

    def _pending_input(self, camera, at):
        cursor = self.db.execute(
            "SELECT cursor FROM daily_cursors WHERE camera=?", (camera,)
        ).fetchone()
        if cursor is not None and cursor[0] > at:
            return False
        if not self.daily._policy(camera)["enabled"] or self.daily._disabled_spans(
            camera, at, at + 0.001
        ):
            return False
        times = [
            r[0]
            for r in self.db.execute(
                "SELECT timestamp FROM frames WHERE camera=? AND timestamp>=? AND timestamp<=?",
                (camera, at - 1, at + 1),
            )
        ]
        queue = self.recording.queue
        if queue is not None:
            times.extend(queue.pending_timestamps(camera))
        latest = self.recording.hub.latest.get(camera)
        if latest is not None:
            times.append(latest.received_at)
        before = sorted({t for t in times if at - 1 <= t <= at})
        after = sorted(t for t in times if at <= t <= at + 1)
        if not before:
            return False
        if after and after[0] - before[-1] <= 1:
            return True
        # Match the recorder's actual observed input cadence, not its output FPS.
        period = before[-1] - before[-2] if len(before) > 1 else 1 / self.daily.settings.daily_fps
        return 0 <= at - before[-1] <= min(1, period) + 0.000001

    async def create_clip(self, camera: str, event: str, event_at: object) -> tuple[dict, bool]:
        at = event_timestamp(event_at)
        if (
            not isinstance(camera, str)
            or not isinstance(event, str)
            or not event.strip()
            or len(event) > 128
        ):
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid camera_id or event_id.")
        self.recording._camera(camera)
        async with self.lock:
            existing = self.db.execute(
                "SELECT body FROM jobs WHERE camera=? AND event=?", (camera, event)
            ).fetchone()
            if existing:
                body = json.loads(existing[0])
                await self.recording._expire(body)
                return body, False
            now = int(self.daily.clock() * 1000) / 1000
            if at > now:
                raise ApiError(400, "EVENT_TIME_IN_FUTURE", "event_at must not be in the future.")
            count = self.db.execute(
                "SELECT count(*) FROM jobs WHERE status IN (?,?)", ACTIVE
            ).fetchone()[0]
            if count >= self.recording.max_jobs:
                raise ApiError(503, "SERVICE_BUSY", "Too many active recordings.")
            policy = self.recording._policy(camera)
            start, end = at - policy["pre_roll_seconds"], at + policy["post_roll_seconds"]
            slices, gaps = self._select(camera, at, now, start, end)
            reserve = int(sum(s["size_bytes"] for s in slices) * 1.2) + 1048576
            await self.daily.check_space(reserve)
            clip_id = str(uuid.uuid4())
            body = dict(
                clip_id=clip_id,
                camera_id=camera,
                event_id=event,
                event_at=iso(at),
                status="processing",
                coverage="partial" if gaps else "complete",
                accepted_at=iso(now),
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
                available_intervals=[
                    {"start_at": iso(s["clip_start"]), "end_at": iso(s["clip_end"])} for s in slices
                ],
            )
            with self.db:
                self.db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                    (clip_id, camera, event, now, start, end, "processing", json.dumps(body)),
                )
                self.db.execute(
                    "INSERT INTO clip_sources VALUES(?,?)", (clip_id, json.dumps(slices))
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO clip_recording_pins VALUES(?,?)",
                    ((clip_id, s["recording_id"]) for s in slices),
                )
            task = asyncio.create_task(self._process(clip_id, slices))
            self.daily.storage_reservations[clip_id] = reserve
            self.jobs[clip_id] = task
            task.add_done_callback(lambda done, key=clip_id: self._done(key, done))
            return body, True

    def _done(self, key, task):
        self.jobs.pop(key, None)
        if not task.cancelled() and task.exception():
            LOG.error("Recorded clip failed", exc_info=task.exception())

    async def _process(self, clip_id, slices):
        error, metadata = None, None
        try:
            async with self.recording.encoding_slot:
                from camera_media_service.clip_media import remux_clip

                metadata = await remux_clip(
                    self.recording.ffmpeg,
                    self.recording.root,
                    slices,
                    self.recording.clips / f"{clip_id}.mp4",
                )
        except asyncio.CancelledError:
            error = {"code": "JOB_INTERRUPTED", "message": "Clip preparation interrupted."}
        except (OSError, RuntimeError, TimeoutError, ApiError) as exc:
            LOG.exception("Recorded clip preparation failed: %s", clip_id)
            error = {
                "code": exc.code if isinstance(exc, ApiError) else "CLIP_PREPARATION_FAILED",
                "message": "Recorded clip preparation failed.",
            }
        while True:
            try:
                async with self.lock:
                    body = self.recording._get(clip_id)
                    with self.db:
                        if error:
                            body.update(status="failed", error=error)
                        else:
                            now = self.daily.clock()
                            body.update(
                                status="ready",
                                completed_at=iso(now),
                                expires_at=iso(
                                    now + body["recording_policy"]["retention_days"] * 86400
                                ),
                                content_path=f"/api/v1/clips/{clip_id}/content",
                                actual_start_at=iso(metadata["start_at"]),
                                actual_end_at=iso(metadata["end_at"]),
                                available_intervals=metadata["available_intervals"],
                            )
                        self.recording._save(body)
                        self.db.execute(
                            "DELETE FROM clip_recording_pins WHERE clip_id=?", (clip_id,)
                        )
                        self.db.execute("DELETE FROM clip_sources WHERE clip_id=?", (clip_id,))
                self.daily.storage_reservations.pop(clip_id, None)
                break
            except sqlite3.Error:
                LOG.exception("Retrying recorded clip result persistence: %s", clip_id)
                await asyncio.sleep(1)
        if error:
            (self.recording.clips / f"{clip_id}.mp4").unlink(missing_ok=True)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        keys, tasks = list(self.jobs), list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first instruction never enters _process.
        async with self.lock:
            with self.db:
                for key in keys:
                    body = self.recording._get(key)
                    if body["status"] in ACTIVE:
                        body.update(
                            status="failed",
                            error={
                                "code": "JOB_INTERRUPTED",
                                "message": "Clip preparation interrupted.",
                            },
                        )
                        self.recording._save(body)
                    self.db.execute("DELETE FROM clip_recording_pins WHERE clip_id=?", (key,))
                    self.db.execute("DELETE FROM clip_sources WHERE clip_id=?", (key,))
                    self.daily.storage_reservations.pop(key, None)
