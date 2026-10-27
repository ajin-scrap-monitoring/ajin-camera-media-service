"""Continuous daily recording on short, leased windows of the shared JPEG buffer."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable

from camera_media_service.daily_catalog import DailyCatalog
from camera_media_service.daily_gaps import build_gaps
from camera_media_service.daily_models import DailyWindow, seoul_date
from camera_media_service.daily_playback import build_available_intervals
from camera_media_service.encoding import encode_daily_segment, probe_media
from camera_media_service.recording import ApiError, RecordingService, epoch, iso

LOG = logging.getLogger(__name__)
POLICY = {"enabled": True, "retention_days": 30}


class DailyRecordingService:
    def __init__(
        self,
        recording: RecordingService,
        catalog: DailyCatalog,
        *,
        clock: Callable[[], float] | None = None,
        settings,
    ):
        self.recording, self.catalog, self.settings, self.clock = (
            recording,
            catalog,
            settings,
            clock or time.time,
        )
        self.db, self.lock = recording.db, recording.lock
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS daily_cursors("
            "camera TEXT PRIMARY KEY,cursor REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS daily_policy_transitions("
            "camera TEXT NOT NULL,at REAL NOT NULL,enabled INTEGER NOT NULL,"
            "PRIMARY KEY(camera,at));"
        )
        self.initialized = False
        self.task = None
        self.tick_lock = asyncio.Lock()
        self.losses = []
        self.offline = {}
        self.last_error = None
        self._closed = False
        self.storage_reservations = {}

    def notify_loss(self, camera_id: str, start_at: float, end_at: float, reason_code: str) -> None:
        if end_at > start_at and camera_id in self.recording.cameras:
            if len(self.losses) < 10000:
                self.losses.append((camera_id, start_at, end_at, reason_code))
            else:
                self.last_error = {
                    "code": "LOSS_LOG_OVERFLOW",
                    "message": "Loss details exceeded capacity.",
                }

    def connection_changed(self, camera_id: str, connected: bool, observed_at: float) -> None:
        if connected:
            start = self.offline.pop(camera_id, None)
            if start is not None:
                self.notify_loss(camera_id, start, observed_at, "CAMERA_DISCONNECTED")
        else:
            self.offline.setdefault(camera_id, observed_at)

    def _policy(self, camera: str) -> dict:
        row = self.db.execute(
            "SELECT body FROM daily_policies WHERE camera=?", (camera,)
        ).fetchone()
        return json.loads(row[0]) if row else dict(POLICY)

    async def get_policy(self, camera_id: str) -> dict:
        self.recording._camera(camera_id)
        async with self.lock:
            return self._policy(camera_id)

    async def set_policy(self, camera_id: str, body: dict) -> dict:
        self.recording._camera(camera_id)
        if (
            set(body) != set(POLICY)
            or type(body["enabled"]) is not bool
            or type(body["retention_days"]) is not int
            or not 1 <= body["retention_days"] <= 365
        ):
            raise ApiError(
                400, "INVALID_ARGUMENT", "enabled and retention_days (1..365) are required."
            )
        async with self.lock:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO daily_policies VALUES(?,?)",
                    (camera_id, json.dumps(body)),
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO daily_policy_transitions VALUES(?,?,?)",
                    (camera_id, self.clock(), int(body["enabled"])),
                )
        return body

    def _disabled_spans(self, camera, start, end):
        rows = self.db.execute(
            "SELECT at,enabled FROM daily_policy_transitions WHERE camera=? AND at<? ORDER BY at",
            (camera, end),
        ).fetchall()
        enabled, point, spans = True, start, []
        for row in rows:
            if row["at"] <= start:
                enabled = bool(row["enabled"])
                continue
            if not enabled:
                spans.append((point, row["at"]))
            point, enabled = row["at"], bool(row["enabled"])
        if not enabled and point < end:
            spans.append((point, end))
        return spans

    def _ensure_day(self, camera, window, retention):
        day = self.catalog.ensure_day(camera, window, retention)
        if "hls_target_duration" not in day:
            day.update(
                segment_seconds=self.settings.daily_segment_seconds,
                hls_target_duration=self.settings.daily_segment_seconds + 1,
                storage_profile=self.profile,
            )
            self.catalog.save_day(day)
        return day

    def _flush_losses(self, now: float) -> None:
        losses, self.losses = self.losses, []
        for camera, start, end, reason in losses:
            self._save_loss(camera, start, end, reason)
        for camera, start in self.offline.items():
            # Completed windows consult this open interval as well as durable loss records.
            if now > start:
                self._save_loss(camera, start, now, "CAMERA_DISCONNECTED")
                self.offline[camera] = now

    def _save_loss(self, camera, start, end, reason):
        row = self.db.execute(
            "SELECT id,start,end FROM daily_gaps WHERE camera=? AND reason=? "
            "AND start<=? AND end>=? ORDER BY id DESC LIMIT 1",
            (camera, reason, end, start),
        ).fetchone()
        if row:
            self.db.execute(
                "UPDATE daily_gaps SET start=?,end=? WHERE id=?",
                (min(start, row["start"]), max(end, row["end"]), row["id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                (camera, start, end, reason),
            )

    def segment_path(self, segment: dict):
        day = self.catalog.get_day(segment["recording_id"])
        block = max(0, int((segment["start_at"] - epoch(day["window_start_at"])) // 600))
        return (
            self.recording.root
            / "daily"
            / day["recording_id"]
            / f"{block:04d}"
            / (segment["segment_id"] + ".ts")
        )

    async def _recover_segments(self) -> None:
        for row in self.db.execute(
            "SELECT body FROM daily_segments WHERE status='processing'"
        ).fetchall():
            segment = json.loads(row[0])
            path = self.segment_path(segment)
            try:
                metadata = await probe_media(self.recording.ffmpeg, path)
                prepared = json.loads(
                    path.with_suffix(".prepared.json").read_text(encoding="utf-8")
                )
                day = self.catalog.get_day(segment["recording_id"])
                if (
                    not epoch(day["window_start_at"])
                    <= prepared["start_at"]
                    < prepared["end_at"]
                    <= epoch(day["window_end_at"])
                ):
                    raise RuntimeError("Recovery coverage outside day")
                if (
                    prepared["size_bytes"] != metadata["size_bytes"]
                    or abs(prepared["duration"] - metadata["duration"]) > 0.1
                ):
                    raise RuntimeError("Recovery metadata does not match media")
                metadata.update(start_at=prepared["start_at"], end_at=prepared["end_at"])
                metadata.update(
                    path=str(path.relative_to(self.recording.root)),
                    profile=segment.get("profile") or self.profile,
                )
                async with self.lock:
                    with self.db:
                        self.catalog.complete_segment(segment["segment_id"], metadata)
            except (OSError, RuntimeError, ValueError, KeyError):
                async with self.lock:
                    with self.db:
                        self.db.execute(
                            "UPDATE daily_segments SET status='failed' WHERE id=?",
                            (segment["segment_id"],),
                        )
                        day = self.catalog.get_day(segment["recording_id"])
                        self.db.execute(
                            "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                            (
                                day["camera_id"],
                                segment["start_at"],
                                segment["end_at"],
                                "SERVICE_INTERRUPTED",
                            ),
                        )
                path.with_suffix(".tmp.ts").unlink(missing_ok=True)
            finally:
                with self.db:
                    self.recording.release_frames(segment["segment_id"])

    async def _recover(self, now: float) -> None:
        await self._recover_segments()
        async with self.lock:
            with self.db:
                # This singleton recorder owns every daily pin. Old outer leases have no
                # coroutine after restart, so reclaim them once segment recovery is finished.
                self.db.execute(
                    "DELETE FROM frame_pins WHERE owner NOT IN (SELECT id FROM daily_segments)"
                )
                for camera in self.recording.cameras:
                    if not self.db.execute(
                        "SELECT 1 FROM daily_policy_transitions WHERE camera=?", (camera,)
                    ).fetchone():
                        self.db.execute(
                            "INSERT INTO daily_policy_transitions VALUES(?,?,?)",
                            (camera, -1e30, int(self._policy(camera)["enabled"])),
                        )
                for row in self.db.execute("SELECT camera,cursor FROM daily_cursors").fetchall():
                    if row["cursor"] < now:
                        self.db.execute(
                            "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                            (row["camera"], row["cursor"], now, "SERVICE_INTERRUPTED"),
                        )
        self.initialized = True

    @property
    def profile(self) -> dict:
        return {
            "width": getattr(self.settings, "daily_width", 1920),
            "height": getattr(self.settings, "daily_height", 1080),
            "fps": getattr(self.settings, "daily_fps", 30),
        }

    def _refresh(self, day: dict, until: float) -> dict:
        start, end = epoch(day["window_start_at"]), epoch(day["window_end_at"])
        until = max(epoch(day["coverage_as_of_at"]), min(end, until))
        segments = self.catalog.segments(day["recording_id"], ready_only=True)
        offset = 0.0
        for segment in segments:
            offset += segment["duration"]
        spans = []
        point = start
        for segment in segments:
            if segment["start_at"] > point:
                spans.append((point, min(segment["start_at"], until)))
            point = max(point, segment["end_at"])
        if point < until:
            spans.append((point, until))
        known = self.db.execute(
            "SELECT start,end,reason FROM daily_gaps "
            "WHERE camera=? AND start<? AND end>? ORDER BY id",
            (day["camera_id"], until, start),
        ).fetchall()
        gaps = build_gaps(spans, known)
        day.update(
            coverage_as_of_at=iso(until),
            recorded_seconds=round(offset, 3),
            gaps=gaps,
            has_gaps=bool(gaps),
            available_intervals=build_available_intervals(segments, gaps),
            playable_until_at=iso(segments[-1]["end_at"]) if segments else None,
            playback_state="ready" if segments else "pending",
            playback_path=f"/api/v2/daily-recordings/{day['recording_id']}/hls/index.m3u8"
            if segments
            else None,
            error=self.last_error,
        )
        if until >= end:
            day.update(
                status="closed",
                coverage="none" if not segments else "partial" if gaps else "complete",
                expires_at=iso(end + day["retention_days"] * 86400),
                export_allowed=bool(segments),
                playback_state="ready" if segments else "unavailable",
            )
        self.catalog.save_day(day)
        return day

    async def tick(self, now: float) -> None:
        async with self.tick_lock:
            if not self.initialized:
                await self._recover(now)
            if self.recording.queue is not None:
                await self.recording.queue.wait_received()
            async with self.lock:
                with self.db:
                    self._flush_losses(now)
            for camera in sorted(self.recording.cameras):
                async with self.lock:
                    policy = self._policy(camera)
                    row = self.db.execute(
                        "SELECT cursor FROM daily_cursors WHERE camera=?", (camera,)
                    ).fetchone()
                    if row is None:
                        if not policy["enabled"]:
                            continue
                        cursor = now
                        window = DailyWindow.from_date(seoul_date(now))
                        with self.db:
                            day = self._ensure_day(camera, window, policy["retention_days"])
                            if now > window.start_at:
                                self.db.execute(
                                    "INSERT INTO daily_gaps(camera,start,end,reason) "
                                    "VALUES(?,?,?,?)",
                                    (camera, window.start_at, now, "RECORDING_NOT_STARTED"),
                                )
                            self._refresh(day, now)
                            self.db.execute("INSERT INTO daily_cursors VALUES(?,?)", (camera, now))
                    else:
                        cursor = row[0]
                if now < cursor:
                    self.last_error = {
                        "code": "CLOCK_ADJUSTED",
                        "message": "Wall clock moved backwards; recording pauses until safe time.",
                    }
                    async with self.lock:
                        with self.db:
                            for item in self.db.execute(
                                "SELECT body FROM daily_recordings WHERE camera=? "
                                "AND status IN ('recording','finalizing')",
                                (camera,),
                            ).fetchall():
                                paused = json.loads(item[0])
                                paused["error"] = self.last_error
                                self.catalog.save_day(paused)
                    continue
                while cursor < now:
                    window = DailyWindow.from_date(seoul_date(cursor))
                    async with self.lock:
                        with self.db:
                            day = self._ensure_day(camera, window, policy["retention_days"])
                            if now >= window.end_at and day["status"] == "recording":
                                day["status"] = "finalizing"
                                self.catalog.save_day(day)
                    slot_end = min(
                        window.end_at,
                        (math.floor(cursor / day["segment_seconds"]) + 1) * day["segment_seconds"],
                    )
                    if slot_end > now:
                        break
                    if now - cursor > self.settings.max_daily_pending_seconds:
                        slot_end = min(window.end_at, now - self.settings.max_daily_pending_seconds)
                        reason = (
                            "SERVICE_INTERRUPTED"
                            if any(
                                r[0] == "SERVICE_INTERRUPTED"
                                for r in self.db.execute(
                                    "SELECT reason FROM daily_gaps "
                                    "WHERE camera=? AND start<=? AND end>?",
                                    (camera, cursor, cursor),
                                )
                            )
                            else "BUFFER_DROPPED"
                        )
                        self.notify_loss(camera, cursor, slot_end, reason)
                    else:
                        await self._record_slot(day, cursor, slot_end)
                    cursor = slot_end
                    async with self.lock:
                        with self.db:
                            self._flush_losses(now)
                            self._refresh(self.catalog.get_day(day["recording_id"]), cursor)
                            self.db.execute(
                                "UPDATE daily_cursors SET cursor=? WHERE camera=?", (cursor, camera)
                            )
                # Even an empty next day exists immediately at midnight.
                if seoul_date(cursor) == seoul_date(now):
                    async with self.lock:
                        with self.db:
                            self._ensure_day(
                                camera,
                                DailyWindow.from_date(seoul_date(now)),
                                policy["retention_days"],
                            )

    async def _record_slot(self, day: dict, start: float, end: float) -> None:
        lease_id = str(uuid.uuid4())
        async with self.lock:
            with self.db:
                for a, b in self._disabled_spans(day["camera_id"], start, end):
                    self._save_loss(day["camera_id"], a, b, "RECORDING_DISABLED")
                frames = self.recording.pin_frames(lease_id, day["camera_id"], start, end)
                loss = self.db.execute(
                    "SELECT start,end FROM daily_gaps WHERE camera=? AND start<? AND end>?",
                    (day["camera_id"], end, start),
                ).fetchall()
                existing = self.catalog.segments(day["recording_id"], ready_only=True)
                profile = (
                    existing[0]["profile"] if existing else day.get("storage_profile", self.profile)
                )
        groups = []
        for frame in frames:
            if any(s["start_at"] <= frame.received_at < s["end_at"] for s in existing):
                continue
            if any(a <= frame.received_at < b for a, b in loss):
                continue
            previous = groups[-1][-1] if groups else None
            split = previous is not None and (
                frame.received_at - previous.received_at > 1
                or any(a < frame.received_at and b > previous.received_at for a, b in loss)
            )
            if previous is None or split:
                groups.append([])
            groups[-1].append(frame)
        try:
            for group in groups:
                async with self.lock:
                    with self.db:
                        segment = self.catalog.begin_segment(
                            day["recording_id"], group[0].received_at, end
                        )
                        segment["profile"] = profile
                        self.db.execute(
                            "UPDATE daily_segments SET body=? WHERE id=?",
                            (json.dumps(segment), segment["segment_id"]),
                        )
                        self.db.executemany(
                            "INSERT OR IGNORE INTO frame_pins VALUES(?,?)",
                            ((segment["segment_id"], f.path) for f in group),
                        )
                path = self.segment_path(segment)
                try:
                    await self.check_space(sum(f.size_bytes for f in group))
                    group_end = min([end, *[a for a, b in loss if a > group[-1].received_at]])
                    metadata = await encode_daily_segment(
                        self.recording.ffmpeg,
                        self.recording.buffer,
                        path,
                        group,
                        {**profile, "end_at": group_end},
                    )
                    metadata["path"] = str(path.relative_to(self.recording.root))
                    async with self.lock:
                        with self.db:
                            self.catalog.complete_segment(segment["segment_id"], metadata)
                            current = self.catalog.get_day(day["recording_id"])
                            current["revision"] += 1
                            self.catalog.save_day(current)
                    self.last_error = None
                except (OSError, RuntimeError, TimeoutError, ApiError) as exc:
                    LOG.exception("Daily segment failed: %s", segment["segment_id"])
                    code = (
                        exc.code
                        if isinstance(exc, ApiError)
                        else "STORAGE_ERROR"
                        if isinstance(exc, OSError)
                        else "ENCODING_FAILED"
                    )
                    self.notify_loss(
                        day["camera_id"],
                        group[0].received_at,
                        end,
                        "STORAGE_FULL" if code == "INSUFFICIENT_STORAGE" else code,
                    )
                    self.last_error = {
                        "code": code,
                        "message": "Daily recording segment could not be saved.",
                    }
                    async with self.lock:
                        with self.db:
                            self.db.execute(
                                "UPDATE daily_segments SET status='failed' WHERE id=?",
                                (segment["segment_id"],),
                            )
                finally:
                    async with self.lock:
                        with self.db:
                            self.recording.release_frames(segment["segment_id"])
        finally:
            async with self.lock:
                with self.db:
                    self.recording.release_frames(lease_id)

    async def check_space(self, reserve: int = 0) -> None:
        reserve += sum(self.storage_reservations.values())

        def usage():
            total = sum(
                p.stat().st_size
                for folder in ("daily", "daily-exports", "daily-playback")
                for p in (self.recording.root / folder).rglob("*")
                if p.is_file()
            )
            return total, shutil.disk_usage(self.recording.root).free

        used, free = await asyncio.to_thread(usage)
        if (
            used + reserve > self.settings.max_daily_storage_bytes
            or free - reserve < self.settings.min_free_storage_bytes
        ):
            raise ApiError(
                503, "INSUFFICIENT_STORAGE", "Daily storage limit or free-space reserve reached."
            )

    async def get_day(self, recording_id: str) -> dict:
        async with self.lock:
            return self.catalog.get_day(recording_id)

    async def list_days(
        self, camera_id: str, filters: dict, limit: int, after: tuple | None
    ) -> list[dict]:
        self.recording._camera(camera_id)
        clauses = ["camera=?"]
        args = [camera_id]
        for key, op in (("from_date", ">="), ("to_date", "<")):
            if key in filters:
                clauses.append(f"date{op}?")
                args.append(filters[key])
        if "status" in filters:
            clauses.append("status=?")
            args.append(filters["status"])
        else:
            clauses.append("status NOT IN ('deleted','expired')")
        if after:
            clauses.append("date<?")
            args.append(after[0])
        async with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT body FROM daily_recordings WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY date DESC LIMIT ?",
                    (*args, limit),
                )
            ]

    async def playback_snapshot(self, recording_id: str) -> tuple[dict, list[dict]]:
        async with self.lock:
            day = self.catalog.get_day(recording_id)
            if day["status"] in ("expired", "deleted"):
                raise ApiError(
                    410,
                    "DAILY_RECORDING_" + day["status"].upper(),
                    "Recording is no longer available.",
                )
            return day, self.catalog.segments(recording_id, ready_only=True)

    async def start(self) -> None:
        await self.tick(self.clock())
        self.recording.hub.loss_observer = self.notify_loss
        self.recording.hub.connection_observer = self.connection_changed
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.25)
            if self._closed:
                break
            try:
                await self.tick(self.clock())
            except (OSError, RuntimeError, ApiError, sqlite3.Error):
                self.last_error = {
                    "code": "STORAGE_ERROR",
                    "message": "Daily recording is interrupted.",
                }
                LOG.exception("Daily recording tick failed")

    async def _finish_tail(self) -> None:
        if not self.initialized:
            return
        await self._recover_segments()
        now = self.clock()
        # Apply the same bounded catch-up policy before flushing a sub-slot tail.
        await self.tick(now)
        if self.recording.queue is not None:
            await self.recording.queue.wait_received()
        async with self.tick_lock:
            for row in self.db.execute("SELECT camera,cursor FROM daily_cursors").fetchall():
                camera, cursor = row["camera"], row["cursor"]
                while cursor < now:
                    day = self._ensure_day(
                        camera,
                        DailyWindow.from_date(seoul_date(cursor)),
                        self._policy(camera)["retention_days"],
                    )
                    end = min(
                        now,
                        epoch(day["window_end_at"]),
                        (math.floor(cursor / day["segment_seconds"]) + 1) * day["segment_seconds"],
                    )
                    await self._record_slot(day, cursor, end)
                    async with self.lock:
                        with self.db:
                            self._flush_losses(now)
                            self._refresh(self.catalog.get_day(day["recording_id"]), end)
                            self.db.execute(
                                "UPDATE daily_cursors SET cursor=? WHERE camera=?", (end, camera)
                            )
                    cursor = end

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=25)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        try:
            await asyncio.wait_for(self._finish_tail(), timeout=25)
        except (TimeoutError, OSError, RuntimeError, sqlite3.Error):
            LOG.exception("Daily shutdown tail will be recovered on restart")
        self.recording.hub.loss_observer = None
        self.recording.hub.connection_observer = None
