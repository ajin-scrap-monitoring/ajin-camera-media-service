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
from dataclasses import replace
from datetime import date
from pathlib import Path

from camera_media_service.daily_capacity import workspace_requirement
from camera_media_service.daily_catalog import DailyCatalog
from camera_media_service.daily_coverage_tracking import install_coverage_tracking
from camera_media_service.daily_gaps import build_gaps
from camera_media_service.daily_metadata import DailyMetadataWriter
from camera_media_service.daily_models import DailyWindow, seoul_date
from camera_media_service.daily_playback import build_available_intervals
from camera_media_service.encoding import encode_daily_segment, probe_media
from camera_media_service.media_files import storage_io
from camera_media_service.media_usage import iter_regular_files
from camera_media_service.persisted_media_info import persist_generated_info
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
        install_coverage_tracking(self.db)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS daily_cursors("
            "camera TEXT PRIMARY KEY,cursor REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS daily_policy_transitions("
            "camera TEXT NOT NULL,at REAL NOT NULL,enabled INTEGER NOT NULL,"
            "PRIMARY KEY(camera,at));"
        )
        recording.daily_frame_watermarks = {
            row["camera"]: row["cursor"]
            for row in self.db.execute("SELECT camera,cursor FROM daily_cursors")
            if row["camera"] in recording.cameras
        }
        self._slot_sizes = {}
        self.initialized = False
        self.task = None
        self.tick_lock = asyncio.Lock()
        self.losses = []
        self.offline = {}
        self.last_error = None
        self.capacity_pending = {}
        self._capacity_retry_at = {}
        for row in self.db.execute(
            "SELECT d.camera,min(s.start),max(s.end) FROM daily_segments s "
            "JOIN daily_recordings d ON d.id=s.day_id "
            "WHERE s.status='deferred' GROUP BY d.camera"
        ):
            if row[0] in recording.cameras:
                self.capacity_pending[row[0]] = {
                    "start": row[1],
                    "end": row[2],
                    "condition": "unknown",
                }
                recording.daily_frame_watermarks[row[0]] = min(
                    recording.daily_frame_watermarks.get(row[0], row[1]), row[1] - 1
                )
        self.metadata_writer = DailyMetadataWriter(recording, clock=self.clock)
        self._closed = False
        self.storage_reservations = recording.capacity_reservations
        self.storage_reservation_outputs = {}
        recording.capacity_check = self.check_space
        recording.capacity_reservations = self.storage_reservations
        recording.capacity_outputs = self.storage_reservation_outputs

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
            async with self.recording.transaction():
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
        self._slot_sizes[camera, window.recording_date.isoformat()] = day["segment_seconds"]
        if len(self._slot_sizes) > 128:
            self._slot_sizes.pop(next(iter(self._slot_sizes)))
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
        row = self.db.execute(
            "SELECT date FROM daily_recordings WHERE id=?", (segment["recording_id"],)
        ).fetchone()
        if row is None:
            raise ApiError(404, "DAILY_RECORDING_NOT_FOUND", "Daily recording does not exist.")
        window = DailyWindow.from_date(date.fromisoformat(row[0]))
        block = max(0, int((segment["start_at"] - window.start_at) // 600))
        return (
            self.recording.root
            / "daily"
            / segment["recording_id"]
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
                await self.check_space()
                async with self.lock:
                    async with self.recording.transaction():
                        self.catalog.complete_segment(
                            segment["segment_id"], metadata, persist_measurement=False
                        )
                # Recovery must make its fresh identity available before returning;
                # ordinary newly recorded slots use the optional background queue.
                await persist_generated_info(self.recording, path, metadata)
            except (OSError, RuntimeError, ValueError, KeyError, ApiError) as exc:
                capacity = isinstance(exc, ApiError) and exc.code == "INSUFFICIENT_STORAGE"
                if capacity:
                    self.last_error = {
                        "code": exc.code,
                        "message": "Recovered media exceeds storage budget.",
                    }
                async with self.lock:
                    async with self.recording.transaction():
                        self.db.execute(
                            "UPDATE daily_segments SET status='failed' WHERE id=?",
                            (segment["segment_id"],),
                        )
                        day = self.catalog.get_day(segment["recording_id"])
                        buffered = self.db.execute(
                            "SELECT 1 FROM frames WHERE camera=? AND timestamp>=? "
                            "AND timestamp<? LIMIT 1",
                            (day["camera_id"], segment["start_at"], segment["end_at"]),
                        ).fetchone()
                        if buffered:
                            self.recording.diagnostics.count("interrupted_segment_buffered")
                        else:
                            self.db.execute(
                                "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                                (
                                    day["camera_id"],
                                    segment["start_at"],
                                    segment["end_at"],
                                    "STORAGE_FULL" if capacity else "SERVICE_INTERRUPTED",
                                ),
                            )
                path.with_suffix(".tmp.ts").unlink(missing_ok=True)
            finally:
                async with self.lock:
                    async with self.recording.transaction():
                        self.recording.release_frames(segment["segment_id"])

    async def _recover(self, now: float) -> None:
        await self._recover_segments()
        async with self.lock:
            async with self.recording.transaction():
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
                        # Durable JPEGs preceding the interruption remain candidates.
                        # Only the tail after the last observed cadence is absent.
                        recent = self.db.execute(
                            "SELECT timestamp FROM frames WHERE camera=? AND timestamp>=? "
                            "AND timestamp<=? ORDER BY timestamp DESC LIMIT 2",
                            (row["camera"], row["cursor"], now),
                        ).fetchall()
                        missing_start = row["cursor"]
                        if recent:
                            cadence = (
                                recent[0][0] - recent[1][0]
                                if len(recent) > 1 and 0 < recent[0][0] - recent[1][0] <= 1
                                else 1 / self.profile["fps"]
                            )
                            missing_start = min(now, recent[0][0] + cadence)
                        if missing_start < now:
                            self.db.execute(
                                "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                                (row["camera"], missing_start, now, "SERVICE_INTERRUPTED"),
                            )
        self.initialized = True

    @property
    def profile(self) -> dict:
        return {
            "width": getattr(self.settings, "daily_width", 1920),
            "height": getattr(self.settings, "daily_height", 1080),
            "fps": getattr(self.settings, "daily_fps", 30),
        }

    def _coverage_rows(self, day, until):
        start = epoch(day["window_start_at"])
        until = max(epoch(day["coverage_as_of_at"]), min(epoch(day["window_end_at"]), until))
        segments = self.db.execute(
            "SELECT body FROM daily_segments WHERE day_id=? AND status='ready' ORDER BY start,id",
            (day["recording_id"],),
        ).fetchall()
        known = [
            dict(row)
            for row in self.db.execute(
                "SELECT start,end,reason FROM daily_gaps "
                "WHERE camera=? AND start<? AND end>? ORDER BY id",
                (day["camera_id"], until, start),
            )
        ]
        return segments, known

    def _refresh(self, day: dict, until: float) -> dict:
        # Synchronous helper retained for callers already doing offline catalogue work.
        until = max(epoch(day["coverage_as_of_at"]), min(epoch(day["window_end_at"]), until))
        rows, known = self._coverage_rows(day, until)
        segments = [json.loads(row[0]) for row in rows]
        result = self._refresh_values(day, until, segments, known)
        self.catalog.save_day(result)
        self._save_coverage_total(result, segments, until)
        return result

    def _save_coverage_total(self, day, segments, until, prior=None):
        duration = prior[0] if prior else 0.0
        ready_end = prior[2] if prior else epoch(day["window_start_at"])
        for segment in segments:
            duration += segment["duration"]
            ready_end = max(ready_end, segment["end_at"])
        self.db.execute(
            "INSERT OR REPLACE INTO daily_coverage_totals VALUES(?,?,?,?)",
            (day["recording_id"], duration, until, ready_end),
        )
        for table in ("daily_gap_dirty", "daily_segment_dirty"):
            self.db.execute(f"DELETE FROM {table} WHERE day_id=?", (day["recording_id"],))

    def _progress_rows(self, day, until):
        total = self.db.execute(
            "SELECT duration,covered_until,ready_end FROM daily_coverage_totals WHERE day_id=?",
            (day["recording_id"],),
        ).fetchone()
        dirty = self.db.execute(
            "SELECT min(start) FROM (SELECT start FROM daily_gap_dirty WHERE day_id=? "
            "UNION ALL SELECT start FROM daily_segment_dirty WHERE day_id=?)",
            (day["recording_id"], day["recording_id"]),
        ).fetchone()[0]
        if (
            total is None
            or iso(total[1]) != day["coverage_as_of_at"]
            or total[2] > total[1]
            or (dirty is not None and dirty < total[1])
            or not isinstance(day.get("gaps"), list)
            or not isinstance(day.get("available_intervals"), list)
        ):
            rows, known = self._coverage_rows(day, until)
            return rows, known, None
        rows = self.db.execute(
            "SELECT body FROM daily_segments WHERE day_id=? AND status='ready' AND start>=? "
            "ORDER BY start,id",
            (day["recording_id"], total[1]),
        ).fetchall()
        known = [
            dict(row)
            for row in self.db.execute(
                "SELECT start,end,reason FROM daily_gaps "
                "WHERE camera=? AND start<? AND end>? ORDER BY id",
                (day["camera_id"], until, total[1]),
            )
        ]
        return rows, known, tuple(total)

    async def _refresh_async(self, day: dict, until: float) -> dict:
        with self.recording.diagnostics.measure("coverage_refresh"):
            return await self._refresh_snapshot(day, until)

    async def _refresh_snapshot(self, day: dict, until: float) -> dict:
        # The caller retains the catalogue lock across drained DB workers.
        # Slow SD reads and FULL commits must not stop JPEG/network receipt.
        until = max(epoch(day["coverage_as_of_at"]), min(epoch(day["window_end_at"]), until))
        rows, known, prior = await storage_io(self._progress_rows, day, until)

        def calculate():
            segments = [json.loads(row[0]) for row in rows]
            result = (
                self._refresh_append_values(day, until, segments, known, prior)
                if prior is not None
                else self._refresh_values(day, until, segments, known)
            )
            return result, segments

        result, segments = await storage_io(calculate)
        await storage_io(self.catalog.save_day, result, notify=False)
        await storage_io(self._save_coverage_total, result, segments, until, prior)
        return result

    def _refresh_append_values(self, day, until, segments, known, prior):
        duration, point, _ = prior
        spans = []
        for item in segments:
            if item["start_at"] > point:
                spans.append((point, min(item["start_at"], until)))
            point = max(point, item["end_at"])
        if point < until:
            spans.append((point, until))
        tail_gaps = build_gaps(spans, known)
        boundary_gaps = ([day["gaps"][-1]] if day["gaps"] else []) + tail_gaps
        intervals = build_available_intervals(
            segments, boundary_gaps, offset=duration, initial_intervals=day["available_intervals"]
        )
        gaps = list(day["gaps"])
        if (
            gaps
            and tail_gaps
            and gaps[-1]["end_at"] == tail_gaps[0]["start_at"]
            and (gaps[-1]["reason_code"] == tail_gaps[0]["reason_code"])
        ):
            gaps[-1] = {**gaps[-1], "end_at": tail_gaps.pop(0)["end_at"]}
        gaps.extend(tail_gaps)
        for item in segments:
            duration += item["duration"]
        playable = iso(segments[-1]["end_at"]) if segments else day["playable_until_at"]
        day.update(
            coverage_as_of_at=iso(until),
            recorded_seconds=round(duration, 3),
            gaps=gaps,
            has_gaps=bool(gaps),
            available_intervals=intervals,
            playable_until_at=playable,
            playback_state="ready" if playable else "pending",
            playback_path=f"/api/v2/daily-recordings/{day['recording_id']}/hls/index.m3u8"
            if playable
            else None,
            error=self.last_error,
        )
        if until >= epoch(day["window_end_at"]):
            day.update(
                status="closed",
                coverage="none" if not playable else "partial" if gaps else "complete",
                expires_at=iso(epoch(day["window_end_at"]) + day["retention_days"] * 86400),
                export_allowed=bool(playable),
                playback_state="ready" if playable else "unavailable",
            )
        return day

    def _refresh_values(self, day: dict, until: float, segments, known) -> dict:
        start, end = epoch(day["window_start_at"]), epoch(day["window_end_at"])
        until = max(epoch(day["coverage_as_of_at"]), min(end, until))
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
        return day

    async def tick(self, now: float) -> None:
        async with self.tick_lock:
            if not self.initialized:
                await self._recover(now)
            if self.recording.queue is not None:
                await self.recording.queue.wait_received()
            async with self.lock:
                async with self.recording.transaction():
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
                        async with self.recording.transaction():
                            day = await storage_io(
                                self._ensure_day, camera, window, policy["retention_days"]
                            )
                            if now > window.start_at:
                                self.db.execute(
                                    "INSERT INTO daily_gaps(camera,start,end,reason) "
                                    "VALUES(?,?,?,?)",
                                    (camera, window.start_at, now, "RECORDING_NOT_STARTED"),
                                )
                            await self._refresh_async(day, now)
                            self.db.execute("INSERT INTO daily_cursors VALUES(?,?)", (camera, now))
                    else:
                        cursor = row[0]
                self.recording.daily_frame_watermarks[camera] = cursor - (
                    1 if camera in self.capacity_pending else 0
                )
                self.recording.diagnostics.gauge("backlog_seconds", max(0, now - cursor))
                if now < cursor:
                    self.last_error = {
                        "code": "CLOCK_ADJUSTED",
                        "message": "Wall clock moved backwards; recording pauses until safe time.",
                    }
                    async with self.lock:
                        async with self.recording.transaction():
                            for item in self.db.execute(
                                "SELECT body FROM daily_recordings WHERE camera=? "
                                "AND status IN ('recording','finalizing')",
                                (camera,),
                            ).fetchall():
                                paused = self.catalog.decode_day(item[0])
                                paused["error"] = self.last_error
                                self.catalog.save_day(paused)
                    continue
                completed_slots = 0
                while cursor < now and completed_slots < 3:
                    if time.monotonic() < self._capacity_retry_at.get(camera, 0):
                        break
                    window = DailyWindow.from_date(seoul_date(cursor))
                    cached_slot = self._slot_sizes.get((camera, window.recording_date.isoformat()))
                    if (
                        cached_slot
                        and min(window.end_at, (math.floor(cursor / cached_slot) + 1) * cached_slot)
                        > now
                    ):
                        break
                    async with self.lock:
                        async with self.recording.transaction():
                            with self.recording.diagnostics.measure("daily_day_snapshot"):
                                day = await storage_io(
                                    self._ensure_day, camera, window, policy["retention_days"]
                                )
                            if now >= window.end_at and day["status"] == "recording":
                                day["status"] = "finalizing"
                                await storage_io(self.catalog.save_day, day, notify=False)
                    slot_end = min(
                        window.end_at,
                        (math.floor(cursor / day["segment_seconds"]) + 1) * day["segment_seconds"],
                    )
                    if slot_end > now:
                        break
                    empty_end = cursor
                    if now - cursor > self.settings.max_daily_pending_seconds:
                        empty_limit = min(
                            window.end_at,
                            math.floor(now / day["segment_seconds"]) * day["segment_seconds"],
                        )
                        async with self.lock:
                            first = self.db.execute(
                                "SELECT min(timestamp) FROM frames WHERE camera=? "
                                "AND timestamp>=? AND timestamp<?",
                                (
                                    camera,
                                    cursor,
                                    empty_limit,
                                ),
                            ).fetchone()[0]
                        empty_end = empty_limit
                        if first is not None:
                            empty_end = min(
                                empty_end,
                                math.floor(first / day["segment_seconds"]) * day["segment_seconds"],
                            )
                    if empty_end > cursor:
                        self.recording.diagnostics.count("source_frames_missing")
                        slot_end = empty_end
                        async with self.lock:
                            async with self.recording.transaction():
                                for left, right in self._disabled_spans(camera, cursor, slot_end):
                                    self._save_loss(camera, left, right, "RECORDING_DISABLED")
                        LOG.info(
                            "Daily backlog has no stored frames: camera=%s start=%s end=%s",
                            camera,
                            iso(cursor),
                            iso(slot_end),
                        )
                    else:
                        if now - cursor > self.settings.max_daily_pending_seconds:
                            self.recording.diagnostics.count("backlog_frames_preserved")
                        if await self._record_slot(day, cursor, slot_end) is False:
                            async with self.lock:
                                async with self.recording.transaction():
                                    pending_day = await storage_io(
                                        self.catalog.get_day, day["recording_id"]
                                    )
                                    pending_day["error"] = self.last_error
                                    await storage_io(
                                        self.catalog.save_day, pending_day, notify=False
                                    )
                            break
                    cursor = slot_end
                    async with self.lock:
                        async with self.recording.transaction():
                            self._flush_losses(now)
                            with self.recording.diagnostics.measure("daily_day_snapshot"):
                                snapshot = await storage_io(
                                    self.catalog.get_day, day["recording_id"]
                                )
                            await self._refresh_async(snapshot, cursor)
                            self.db.execute(
                                "UPDATE daily_cursors SET cursor=? WHERE camera=?", (cursor, camera)
                            )
                    self.recording.daily_frame_watermarks[camera] = cursor
                    completed_slots += 1
                # Even an empty next day exists immediately at midnight.
                current_key = (camera, seoul_date(now).isoformat())
                if current_key not in self._slot_sizes and (
                    policy["enabled"] or seoul_date(cursor) == seoul_date(now)
                ):
                    async with self.lock:
                        async with self.recording.transaction():
                            await storage_io(
                                self._ensure_day,
                                camera,
                                DailyWindow.from_date(seoul_date(now)),
                                policy["retention_days"],
                            )

    async def _record_slot(self, day: dict, start: float, end: float) -> bool:
        lease_id = str(uuid.uuid4())
        async with self.lock:
            async with self.recording.transaction():
                for a, b in self._disabled_spans(day["camera_id"], start, end):
                    self._save_loss(day["camera_id"], a, b, "RECORDING_DISABLED")
                # Lease boundary neighbours too: a slot cut must preserve the same
                # sample-and-hold interval as the original adjacent JPEG receipts.
                nearby = self.recording.pin_frames(lease_id, day["camera_id"], start - 1, end + 1)
                loss = self.db.execute(
                    "SELECT start,end FROM daily_gaps WHERE camera=? AND start<? AND end>?",
                    (day["camera_id"], end + 1, start - 1),
                ).fetchall()
                existing = [
                    json.loads(row[0])
                    for row in self.db.execute(
                        "SELECT body FROM daily_segments WHERE day_id=? AND status='ready' "
                        "AND start<? AND end>? ORDER BY start,id",
                        (day["recording_id"], end, start),
                    )
                ]
                first = self.db.execute(
                    "SELECT body FROM daily_segments WHERE day_id=? AND status='ready' "
                    "ORDER BY start,id LIMIT 1",
                    (day["recording_id"],),
                ).fetchone()
                profile = (
                    json.loads(first[0])["profile"]
                    if first
                    else day.get("storage_profile", self.profile)
                )
        frames = [frame for frame in nearby if start <= frame.received_at < end]
        before = next((frame for frame in reversed(nearby) if frame.received_at < start), None)
        after = next((frame for frame in nearby if frame.received_at >= end), None)

        def contiguous(left, right):
            return (
                left is not None
                and right is not None
                and 0 < right.received_at - left.received_at <= 1
                and not any(a < right.received_at and b > left.received_at for a, b in loss)
            )

        if frames and contiguous(before, frames[0]):
            frames.insert(0, replace(before, received_at=start))
        verified_tail = bool(frames and contiguous(frames[-1], after))
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
                    async with self.recording.transaction():
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
                metadata = None
                admission_stage = "before_encode"
                try:
                    owner = segment["segment_id"]
                    self.storage_reservations[owner] = max(
                        sum(f.size_bytes for f in group), segment.get("capacity_reserve_bytes", 0)
                    )
                    self.storage_reservation_outputs[owner] = (path, path.with_suffix(".tmp.ts"))
                    await self.check_space(recording=True)
                    group_end = min([end, *[a for a, b in loss if a > group[-1].received_at]])
                    with self.recording.diagnostics.measure("daily_encode"):
                        metadata = await encode_daily_segment(
                            self.recording.ffmpeg,
                            self.recording.buffer,
                            path,
                            group,
                            {
                                **profile,
                                "end_at": group_end,
                                "_storage_guard": getattr(self.recording, "storage_guard", None),
                                "_diagnostics": self.recording.diagnostics,
                                **(
                                    {"verified_end_at": end}
                                    if verified_tail and group is groups[-1]
                                    else {}
                                ),
                            },
                        )
                    # JPEG input size does not bound scaled H264/mux output.
                    # Count the finished file before making it available/recoverable.
                    self.storage_reservations[owner] = 0
                    admission_stage = "before_publication"
                    await self.check_space()
                    metadata["path"] = str(path.relative_to(self.recording.root))
                    async with self.lock:
                        async with self.recording.transaction():
                            self.catalog.complete_segment(
                                segment["segment_id"], metadata, persist_measurement=False
                            )
                    self.recording.notify_thumbnail_ready("day", day["recording_id"], metadata)
                    self.metadata_writer.offer(path, metadata)
                    self.last_error = None
                except (OSError, RuntimeError, TimeoutError, ApiError) as exc:
                    capacity = isinstance(exc, ApiError) and exc.code in (
                        "INSUFFICIENT_STORAGE",
                        "CAPACITY_SNAPSHOT_BUSY",
                    )
                    if not capacity:
                        LOG.exception("Daily segment failed: %s", segment["segment_id"])

                    def discard_new_output(path=path):
                        self.recording.storage_guard()
                        path.unlink(missing_ok=True)
                        path.with_suffix(".prepared.json").unlink(missing_ok=True)

                    try:
                        await storage_io(discard_new_output)
                    except OSError:
                        LOG.exception("Rejected segment cleanup deferred")
                    if capacity:
                        segment.update(
                            status="deferred",
                            capacity_reserve_bytes=max(
                                sum(f.size_bytes for f in group),
                                (metadata or {}).get("size_bytes", 0),
                                segment.get("capacity_reserve_bytes", 0),
                            ),
                        )
                        async with self.lock:
                            async with self.recording.transaction():
                                self.db.execute(
                                    "UPDATE daily_segments SET status='deferred',body=? WHERE id=?",
                                    (json.dumps(segment), segment["segment_id"]),
                                )
                        self.capacity_pending[day["camera_id"]] = {
                            "start": start,
                            "end": end,
                            "condition": getattr(exc, "condition", "unknown"),
                        }
                        self._capacity_retry_at[day["camera_id"]] = time.monotonic() + 2
                        self.recording.daily_frame_watermarks[day["camera_id"]] = start - 1
                        self.last_error = {
                            "code": exc.code,
                            "message": "Recording awaits capacity; source frames remain pending.",
                        }
                        self.recording.diagnostics.count("daily_capacity_deferred")
                        LOG.warning(
                            "Daily segment capacity deferred: camera=%s start=%s "
                            "end=%s stage=%s budget=%s",
                            day["camera_id"],
                            iso(start),
                            iso(end),
                            admission_stage,
                            json.dumps(getattr(exc, "budget", {}), sort_keys=True),
                        )
                        return False
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
                        async with self.recording.transaction():
                            self.db.execute(
                                "UPDATE daily_segments SET status='failed' WHERE id=?",
                                (segment["segment_id"],),
                            )
                finally:
                    self.storage_reservations.pop(segment["segment_id"], None)
                    self.storage_reservation_outputs.pop(segment["segment_id"], None)
                    async with self.lock:
                        async with self.recording.transaction():
                            self.recording.release_frames(segment["segment_id"])
        finally:
            async with self.lock:
                async with self.recording.transaction():
                    self.recording.release_frames(lease_id)
        if day["camera_id"] in self.capacity_pending:
            async with self.lock:
                async with self.recording.transaction():
                    self.db.execute(
                        "UPDATE daily_segments SET status='failed', "
                        "body=json_set(body,'$.status','failed') "
                        "WHERE day_id=? AND status='deferred' AND start<? AND end>?",
                        (day["recording_id"], end, start),
                    )
            if self.last_error and self.last_error["code"] in (
                "INSUFFICIENT_STORAGE",
                "CAPACITY_SNAPSHOT_BUSY",
            ):
                self.last_error = None
        self.capacity_pending.pop(day["camera_id"], None)
        self._capacity_retry_at.pop(day["camera_id"], None)
        return True

    async def check_space(self, reserve: int = 0, *, recording: bool = False) -> None:
        with self.recording.diagnostics.measure("space_check"):
            await self._check_space(reserve, recording=recording)

    async def _check_space(self, reserve: int = 0, *, recording: bool = False, _attempt=0) -> None:
        # Snapshot on the event loop; file workers must not read mutable job maps.
        generation = self.recording._budget_generation
        reservations = dict(self.storage_reservations)
        output_owners = {
            str(path): owner
            for owner in reservations
            for path in self.storage_reservation_outputs.get(owner, ())
        }
        total_limit = getattr(self.settings, "max_media_storage_bytes", None)
        workspace = 0
        workspace_floor = getattr(self.settings, "daily_archive_workspace_bytes", 0)
        if recording and workspace_floor and getattr(self.settings, "daily_archive_enabled", True):
            active_archives = {
                key
                for key, paths in self.storage_reservation_outputs.items()
                if any(Path(p).parent.name == "daily-exports" for p in paths)
            }
            # One archive runs at a time. Its reservation/output already occupies
            # the working room; do not reserve a *second* full archive during its
            # read-only verification. Success compacts TS, failure removes MP4.
            workspace = 0 if active_archives else None

        def usage():
            guard = getattr(self.recording, "storage_guard", None)
            if guard is not None:
                guard()
            total = media_total = buffer_bytes = 0
            allocated = dict.fromkeys(reservations, 0)
            categories = ("daily", "daily-exports", "daily-playback", "daily-thumbnails")
            roots = (
                (self.recording.root,)
                if total_limit
                else tuple(self.recording.root / folder for folder in categories)
            )
            for root in roots:
                top_category = None if root == self.recording.root else root.name
                for path, info, category in iter_regular_files(root, category=top_category):
                    media_total += info.st_size
                    if category in categories:
                        total += info.st_size
                    if category == "buffer":
                        buffer_bytes += info.st_size
                    owner = output_owners.get(path)
                    if owner is not None:
                        # Credit only bytes included above, from this same stat.
                        allocated[owner] += info.st_size
            remaining = sum(
                max(0, planned - allocated[owner]) for owner, planned in reservations.items()
            )
            buffer_headroom = (
                max(0, self.recording.max_buffer_bytes - buffer_bytes) if total_limit else 0
            )
            required_workspace = workspace
            if required_workspace is None:
                required_workspace = workspace_requirement(self.recording.root, workspace_floor)
            if guard is not None:
                guard()
            return (
                total,
                media_total,
                shutil.disk_usage(self.recording.root).free,
                remaining,
                buffer_headroom,
                required_workspace,
            )

        used, media_used, free, remaining, buffer_headroom, workspace = await storage_io(usage)
        if generation != self.recording._budget_generation:
            if _attempt >= 2:
                error = ApiError(
                    503, "CAPACITY_SNAPSHOT_BUSY", "Storage reservations changed during admission."
                )
                error.condition = "reservation_changed"
                error.budget = {"condition": error.condition, "attempts": _attempt + 1}
                raise error
            return await self._check_space(reserve, recording=recording, _attempt=_attempt + 1)
        reserve += remaining
        conditions = (
            ("daily_budget", used + reserve + workspace > self.settings.max_daily_storage_bytes),
            (
                "total_budget",
                total_limit and media_used + reserve + workspace + buffer_headroom > total_limit,
            ),
            (
                "minimum_free",
                free - reserve - workspace - buffer_headroom < self.settings.min_free_storage_bytes,
            ),
        )
        for condition, rejected in conditions:
            if rejected:
                self.recording.diagnostics.count("storage_rejected_" + condition)
                error = ApiError(
                    503,
                    "INSUFFICIENT_STORAGE",
                    "Daily storage limit or free-space reserve reached.",
                )
                error.condition = condition
                error.budget = {
                    "condition": condition,
                    "daily_used_bytes": used,
                    "media_used_bytes": media_used,
                    "filesystem_free_bytes": free,
                    "unallocated_reserved_bytes": remaining,
                    "additional_reserve_bytes": reserve - remaining,
                    "workspace_bytes": workspace,
                    "buffer_headroom_bytes": buffer_headroom,
                    "daily_limit_bytes": self.settings.max_daily_storage_bytes,
                    "media_limit_bytes": total_limit,
                    "minimum_free_bytes": self.settings.min_free_storage_bytes,
                }
                raise error

    async def get_day(self, recording_id: str) -> dict:
        async with self.lock:
            return await storage_io(self.catalog.get_day, recording_id)

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

            def read():
                return [
                    self.catalog.decode_day(r[0])
                    for r in self.db.execute(
                        "SELECT body FROM daily_recordings WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY date DESC LIMIT ?",
                        (*args, limit),
                    )
                ]

            return await storage_io(read)

    async def playback_snapshot(self, recording_id: str) -> tuple[dict, list[dict]]:
        async with self.lock:
            day = await storage_io(self.catalog.get_day, recording_id)
            if day["status"] in ("expired", "deleted"):
                raise ApiError(
                    410,
                    "DAILY_RECORDING_" + day["status"].upper(),
                    "Recording is no longer available.",
                )

            def read_segments():
                rows = self.db.execute(
                    "SELECT body FROM daily_segments WHERE day_id=? AND status='ready' "
                    "ORDER BY start,id",
                    (recording_id,),
                ).fetchall()
                return [json.loads(row[0]) for row in rows]

            segments = await storage_io(read_segments)
            return day, segments

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
                completed_slots = 0
                while cursor < now and completed_slots < 3:
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
                    if await self._record_slot(day, cursor, end) is False:
                        break
                    async with self.lock:
                        async with self.recording.transaction():
                            self._flush_losses(now)
                            await self._refresh_async(
                                self.catalog.get_day(day["recording_id"]), end
                            )
                            self.db.execute(
                                "UPDATE daily_cursors SET cursor=? WHERE camera=?", (end, camera)
                            )
                    cursor = end
                    self.recording.daily_frame_watermarks[camera] = cursor
                    completed_slots += 1

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.metadata_writer.stop()
        try:
            await self._close_daily()
        finally:
            await self.metadata_writer.close()

    async def _close_daily(self) -> None:
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
