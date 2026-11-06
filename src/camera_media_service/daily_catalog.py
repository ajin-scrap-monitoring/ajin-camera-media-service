"""Daily metadata. Callers own RecordingService's connection, lock and transaction."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from camera_media_service.daily_models import DailyWindow
from camera_media_service.persisted_media_info import record_generated_info
from camera_media_service.recording import ApiError, iso


class DailyCatalog:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        database_path = db.execute("PRAGMA database_list").fetchone()[2]
        self.root = Path(database_path).parent if database_path else None
        db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_recordings(
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, date TEXT NOT NULL,
                status TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(camera,date));
            CREATE TABLE IF NOT EXISTS daily_revisions(
                day_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_snapshot_deltas(
                sequence INTEGER PRIMARY KEY, day_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_states(day_id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS daily_snapshot_day
                ON daily_snapshot_deltas(day_id,sequence);
            CREATE TABLE IF NOT EXISTS daily_policies(camera TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_segments(
                id TEXT PRIMARY KEY, day_id TEXT NOT NULL, start REAL NOT NULL,
                end REAL NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS daily_segment_time ON daily_segments(day_id,start,id);
            CREATE TABLE IF NOT EXISTS daily_gaps(
                id INTEGER PRIMARY KEY, camera TEXT NOT NULL, start REAL NOT NULL,
                end REAL NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS daily_gap_time ON daily_gaps(camera,start,end);
            CREATE INDEX IF NOT EXISTS daily_gap_recent ON daily_gaps(camera,reason,end,start);
            CREATE INDEX IF NOT EXISTS daily_gap_end ON daily_gaps(camera,end,start);
            CREATE TABLE IF NOT EXISTS daily_exports(
                day_id TEXT PRIMARY KEY, status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clip_recording_pins(
                clip_id TEXT NOT NULL,day_id TEXT NOT NULL,PRIMARY KEY(clip_id,day_id));
        """)
        # An older binary may have advanced the legacy body during an image rollback.
        # Reconcile small counters once at startup, never read the large body per segment.
        with db:
            db.execute(
                "UPDATE daily_revisions SET revision=(SELECT json_extract(body,'$.revision') "
                "FROM daily_recordings WHERE id=day_id) WHERE revision < "
                "(SELECT json_extract(body,'$.revision') FROM daily_recordings WHERE id=day_id)"
            )
            # Run only before writers start: move legacy large arrays out of the
            # row whose status is updated at midnight/deletion. SQLite rewrites
            # overflow pages even for a status-only UPDATE on a large row.
            keys = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM daily_recordings WHERE length(body)>65536 OR id IN "
                    "(SELECT day_id FROM daily_snapshot_deltas "
                    "GROUP BY day_id HAVING count(*)>1024)"
                )
            ]
            for key in keys:
                body = self.get_day(key)
                arrays = {
                    name: {"keep": 0, "append": body[name]}
                    for name in ("gaps", "available_intervals")
                    if name in body
                }
                base = {name: [] if name in arrays else value for name, value in body.items()}
                db.execute(
                    "UPDATE daily_recordings SET status=?,body=? WHERE id=?",
                    (body["status"], json.dumps(base), key),
                )
                db.execute("DELETE FROM daily_snapshot_deltas WHERE day_id=?", (key,))
                if arrays:
                    db.execute(
                        "INSERT INTO daily_snapshot_deltas(day_id,body) VALUES(?,?)",
                        (key, json.dumps({"values": {}, "arrays": arrays})),
                    )
            # An older image may have changed the materialized body during rollback.
            for row in db.execute("SELECT body FROM daily_recordings"):
                self._save_state(self.decode_day(row[0]))

    def _save_state(self, body):
        state = {k: v for k, v in body.items() if k not in ("gaps", "available_intervals")}
        self.db.execute(
            "INSERT OR REPLACE INTO daily_states VALUES(?,?)",
            (body["recording_id"], json.dumps(state)),
        )

    def get_state(self, recording_id):
        row = self.db.execute(
            "SELECT body FROM daily_states WHERE day_id=?", (recording_id,)
        ).fetchone()
        if row is None:
            return self.get_day(recording_id)
        return json.loads(row[0])

    def ensure_day(self, camera_id: str, window: DailyWindow, retention_days: int) -> dict:
        row = self.db.execute(
            "SELECT body FROM daily_recordings WHERE camera=? AND date=?",
            (camera_id, window.recording_date.isoformat()),
        ).fetchone()
        if row:
            return self.decode_day(row[0])
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
        self._save_state(body)
        return body

    def get_day(self, recording_id: str) -> dict:
        row = self.db.execute(
            "SELECT body FROM daily_recordings WHERE id=?", (recording_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "DAILY_RECORDING_NOT_FOUND", "Daily recording does not exist.")
        return self.decode_day(row[0])

    def decode_day(self, raw):
        body = json.loads(raw)
        for row in self.db.execute(
            "SELECT body FROM daily_snapshot_deltas WHERE day_id=? ORDER BY sequence",
            (body["recording_id"],),
        ):
            patch = json.loads(row[0])
            for key in patch.get("remove", ()):
                body.pop(key, None)
            body.update(patch["values"])
            for key, change in patch["arrays"].items():
                values = body.setdefault(key, [])
                del values[change["keep"] :]
                values.extend(change["append"])
        row = self.db.execute(
            "SELECT revision FROM daily_revisions WHERE day_id=?", (body["recording_id"],)
        ).fetchone()
        if row:
            body["revision"] = max(body.get("revision", 0), row[0])
        return body

    def advance_revision(self, key):
        row = self.db.execute(
            "SELECT revision FROM daily_revisions WHERE day_id=?", (key,)
        ).fetchone()
        if row is None:
            prior = self.db.execute(
                "SELECT coalesce(json_extract(body,'$.revision'),0) "
                "FROM daily_recordings WHERE id=?",
                (key,),
            ).fetchone()[0]
            self.db.execute("INSERT INTO daily_revisions VALUES(?,?)", (key, prior + 1))
        else:
            self.db.execute("UPDATE daily_revisions SET revision=revision+1 WHERE day_id=?", (key,))

    def save_day(self, body: dict, *, notify: bool = True) -> None:
        row = self.db.execute(
            "SELECT revision FROM daily_revisions WHERE day_id=?", (body["recording_id"],)
        ).fetchone()
        if row:
            body["revision"] = max(body.get("revision", 0), row[0])
        prior = self.get_day(body["recording_id"])
        arrays = {}
        for key in ("gaps", "available_intervals"):
            previous, current = prior.get(key, []), body.get(key, [])
            keep = 0
            for left, right in zip(previous, current, strict=False):
                if left != right:
                    break
                keep += 1
            if keep != len(previous) or keep != len(current):
                arrays[key] = {"keep": keep, "append": current[keep:]}
        values = {
            key: value
            for key, value in body.items()
            if key not in ("gaps", "available_intervals")
            and (key not in prior or prior[key] != value)
        }
        remove = list(prior.keys() - body.keys())
        if arrays or values or remove:
            self.db.execute(
                "INSERT INTO daily_snapshot_deltas(day_id,body) VALUES(?,?)",
                (
                    body["recording_id"],
                    json.dumps({"values": values, "arrays": arrays, "remove": remove}),
                ),
            )
            self._save_state(body)
        if prior["status"] != body["status"]:
            self.db.execute(
                "UPDATE daily_recordings SET status=? WHERE id=?",
                (body["status"], body["recording_id"]),
            )

        observer = getattr(self, "thumbnail_change_observer", None)
        if observer and notify:
            observer("day", body["recording_id"], body["status"])

    def materialize_snapshots(self) -> None:
        """Caller stops all writers and owns the transaction before an old-image rollback."""
        # Include a ready segment committed just before a crash, when only the
        # revision counter advanced and no coverage patch exists yet.
        keys = [r[0] for r in self.db.execute("SELECT id FROM daily_recordings")]
        for key in keys:
            body = self.get_day(key)
            self.db.execute(
                "UPDATE daily_recordings SET status=?,body=? WHERE id=?",
                (body["status"], json.dumps(body), key),
            )
            self.db.execute("DELETE FROM daily_snapshot_deltas WHERE day_id=?", (key,))

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
        if self.root is not None and metadata.get("path"):
            record_generated_info(self.db, self.root, self.root / metadata["path"], metadata)
        if body["status"] != "ready":
            self.advance_revision(body["recording_id"])
        body.update({k: v for k, v in metadata.items() if not k.startswith("_")}, status="ready")
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
