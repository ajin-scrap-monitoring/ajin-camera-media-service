"""Daily metadata. Callers own RecordingService's connection, lock and transaction."""

from __future__ import annotations

import json
import sqlite3
import uuid

from camera_media_service.daily_models import DailyWindow
from camera_media_service.recording import ApiError, iso


class DailyCatalog:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_recordings(
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, date TEXT NOT NULL,
                status TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(camera,date));
            CREATE TABLE IF NOT EXISTS daily_policies(camera TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_segments(
                id TEXT PRIMARY KEY, day_id TEXT NOT NULL, start REAL NOT NULL,
                end REAL NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS daily_segment_time ON daily_segments(day_id,start,id);
            CREATE TABLE IF NOT EXISTS daily_gaps(
                id INTEGER PRIMARY KEY, camera TEXT NOT NULL, start REAL NOT NULL,
                end REAL NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS daily_gap_time ON daily_gaps(camera,start,end);
            CREATE TABLE IF NOT EXISTS daily_exports(
                day_id TEXT PRIMARY KEY, status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clip_recording_pins(
                clip_id TEXT NOT NULL,day_id TEXT NOT NULL,PRIMARY KEY(clip_id,day_id));
        """)

    def ensure_day(self, camera_id: str, window: DailyWindow, retention_days: int) -> dict:
        row = self.db.execute(
            "SELECT body FROM daily_recordings WHERE camera=? AND date=?",
            (camera_id, window.recording_date.isoformat()),
        ).fetchone()
        if row:
            return json.loads(row[0])
        body = {
            "recording_id": str(uuid.uuid4()),
            "camera_id": camera_id,
            "recording_date": window.recording_date.isoformat(),
            "timezone": "Asia/Seoul",
            "window_start_at": iso(window.start_at),
            "window_end_at": iso(window.end_at),
            "status": "recording",
            "coverage": None,
            "has_gaps": False,
            "coverage_as_of_at": iso(window.start_at),
            "playable_until_at": None,
            "recorded_seconds": 0.0,
            "gaps": [],
            "available_intervals": [],
            "playback_state": "pending",
            "playback_protocol": "hls",
            "playback_path": None,
            "export_allowed": False,
            "export_state": "not_requested",
            "archive_state": "segments",
            "download_path": None,
            "size_bytes": None,
            "expires_at": None,
            "export_expires_at": None,
            "error": None,
            "retention_days": retention_days,
            "revision": 0,
            "deleted_at": None,
        }
        self.db.execute(
            "INSERT INTO daily_recordings VALUES(?,?,?,?,?)",
            (
                body["recording_id"],
                camera_id,
                body["recording_date"],
                "recording",
                json.dumps(body),
            ),
        )
        return body

    def get_day(self, recording_id: str) -> dict:
        row = self.db.execute(
            "SELECT body FROM daily_recordings WHERE id=?", (recording_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "DAILY_RECORDING_NOT_FOUND", "Daily recording does not exist.")
        return json.loads(row[0])

    def save_day(self, body: dict) -> None:
        self.db.execute(
            "UPDATE daily_recordings SET status=?,body=? WHERE id=?",
            (body["status"], json.dumps(body), body["recording_id"]),
        )

    def begin_segment(self, recording_id: str, start_at: float, end_at: float) -> dict:
        body = {
            "segment_id": str(uuid.uuid4()),
            "recording_id": recording_id,
            "start_at": start_at,
            "end_at": end_at,
            "status": "processing",
            "duration": None,
            "size_bytes": None,
            "path": None,
            "profile": None,
        }
        self.db.execute(
            "INSERT INTO daily_segments VALUES(?,?,?,?,?,?)",
            (body["segment_id"], recording_id, start_at, end_at, "processing", json.dumps(body)),
        )
        return body

    def complete_segment(self, segment_id: str, metadata: dict) -> None:
        row = self.db.execute(
            "SELECT body FROM daily_segments WHERE id=?", (segment_id,)
        ).fetchone()
        body = json.loads(row[0])
        body.update(metadata, status="ready")
        self.db.execute(
            "UPDATE daily_segments SET start=?,end=?,status=?,body=? WHERE id=?",
            (body["start_at"], body["end_at"], "ready", json.dumps(body), segment_id),
        )

    def segments(self, recording_id: str, *, ready_only: bool = False) -> list[dict]:
        extra = " AND status='ready'" if ready_only else ""
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT body FROM daily_segments WHERE day_id=?" + extra + " ORDER BY start,id",
                (recording_id,),
            )
        ]
