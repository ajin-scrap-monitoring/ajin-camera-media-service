"""Bounded public catalogue reads, without rewriting stored timelines or media."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from camera_media_service.recording import ApiError
from camera_media_service.snapshot_stream import SnapshotJSON, SnapshotJSONError

ARRAYS = {"gaps", "available_intervals"}
PUBLIC = set(
    """
recording_id camera_id recording_date timezone window_start_at window_end_at status
coverage has_gaps coverage_as_of_at playable_until_at recorded_seconds playback_state
playback_protocol playback_path export_allowed export_state archive_state download_path
size_bytes expires_at export_expires_at error retention_days revision deleted_at
cleanup_state cleanup_completed_at cleanup_error space_reclaimed archive_retry_at
media_info
deletion_reason
segment_seconds hls_target_duration storage_profile
""".split()
)
RESPONSE_BYTES = 4 * 1024 * 1024
STRIDE = 1024


def too_large():
    return ApiError(
        503,
        "DAILY_RESPONSE_TOO_LARGE",
        "Use the daily recording summary and paged gaps/available-intervals APIs.",
    )


@dataclass
class Run:
    source: tuple[str, int]
    offsets: list[int]
    count: int


def _array(reader, source, *, indexed=True):
    offsets, count = [], 0
    reader.take("[")
    if reader.peek() != "]":
        while True:
            reader.peek()
            if indexed and count % STRIDE == 0:
                if len(offsets) >= 16384:
                    raise SnapshotJSONError("Snapshot index limit exceeded")
                offsets.append(reader.offset)
            reader.value()
            count += 1
            if reader.peek() == "]":
                break
            reader.take(",")
    reader.take("]")
    return Run(source, offsets, count)


def _keep(runs, count):
    if type(count) is not int or count < 0:
        raise SnapshotJSONError("Invalid snapshot suffix")
    result = []
    for run in runs:
        length = min(count, run.count)
        if length:
            result.append(Run(run.source, run.offsets[: (length + STRIDE - 1) // STRIDE], length))
        count -= length
    return result


def scan_snapshot(db, recording_id, *, arrays=True):
    """Caller owns a consistent read transaction. Never SELECT a large JSON body."""
    base = db.execute("SELECT rowid FROM daily_recordings WHERE id=?", (recording_id,)).fetchone()
    if base is None:
        raise ApiError(404, "DAILY_RECORDING_NOT_FOUND", "Daily recording does not exist.")
    state, runs = {}, {key: [] for key in ARRAYS}
    sources = [("daily_recordings", base[0])] + [
        ("daily_snapshot_deltas", row[0])
        for row in db.execute(
            "SELECT sequence FROM daily_snapshot_deltas WHERE day_id=? ORDER BY sequence",
            (recording_id,),
        )
    ]
    if len(sources) > 65536:
        raise SnapshotJSONError("Snapshot patch limit exceeded")
    for table, rowid in sources:
        with db.blobopen(table, "body", rowid, readonly=True) as blob:
            reader = SnapshotJSON(blob, max_value_bytes=65536)
            for key in reader.object_keys():
                if table == "daily_recordings":
                    if key in PUBLIC:
                        state[key] = reader.value()
                    elif key in ARRAYS:
                        if arrays:
                            runs[key] = [_array(reader, (table, rowid))]
                        else:
                            _array(reader, (table, rowid), indexed=False)
                    else:
                        reader.skip()
                elif key == "values":
                    for name in reader.object_keys():
                        if name in PUBLIC:
                            state[name] = reader.value()
                        else:
                            reader.skip()
                elif key == "remove":
                    for _, name in reader.array_offsets():
                        state.pop(name, None)
                        if name in runs:
                            runs[name] = []
                elif key == "arrays":
                    for name in reader.object_keys():
                        if name not in ARRAYS:
                            reader.skip()
                            continue
                        keep, append = 0, None
                        for part in reader.object_keys():
                            if part == "keep":
                                keep = reader.value()
                            elif part == "append":
                                if arrays:
                                    append = _array(reader, (table, rowid))
                                else:
                                    _array(reader, (table, rowid), indexed=False)
                            else:
                                reader.skip()
                        runs[name] = _keep(runs[name], keep)
                        if append and append.count:
                            runs[name].append(append)
                        if sum(len(r.offsets) for group in runs.values() for r in group) > 32768:
                            raise SnapshotJSONError("Snapshot index limit exceeded")
                else:
                    reader.skip()
            reader.finish()
    revision = db.execute(
        "SELECT revision FROM daily_revisions WHERE day_id=?", (recording_id,)
    ).fetchone()
    if revision:
        state["revision"] = max(state.get("revision", 0), revision[0])
    return state, runs


class DailySnapshots:
    """One admitted worker owns this instance; at most two sparse indices are cached."""

    def __init__(self, path, *, response_bytes=RESPONSE_BYTES):
        self.path = Path(path)
        self.response_bytes = response_bytes
        self.cache = OrderedDict()
        self.index_builds = 0

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA cache_size=-2048")
            db.execute("BEGIN")
            yield db
        finally:
            db.rollback()
            db.close()

    def _snapshot(self, db, key):
        base = db.execute(
            "SELECT rowid,octet_length(body) FROM daily_recordings WHERE id=?", (key,)
        ).fetchone()
        if base is None:
            raise ApiError(404, "DAILY_RECORDING_NOT_FOUND", "Daily recording does not exist.")
        patches = db.execute(
            "SELECT sequence,octet_length(body) FROM daily_snapshot_deltas "
            "WHERE day_id=? ORDER BY sequence",
            (key,),
        ).fetchall()
        if len(patches) > 65536:
            raise SnapshotJSONError("Snapshot patch limit exceeded")
        revision = db.execute(
            "SELECT revision FROM daily_revisions WHERE day_id=?", (key,)
        ).fetchone()
        return hashlib.sha256(repr((key, base, patches, revision)).encode()).hexdigest()

    def _index(self, db, key, fingerprint):
        cached = self.cache.get(fingerprint)
        if cached is None:
            cached = scan_snapshot(db, key)
            self.cache[fingerprint] = cached
            self.index_builds += 1
            while len(self.cache) > 2:
                self.cache.popitem(last=False)
        self.cache.move_to_end(fingerprint)
        return cached

    def _state(self, db, key, fingerprint):
        has_states = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_states'"
        ).fetchone()
        if has_states:
            row = db.execute(
                "SELECT CASE WHEN octet_length(body)<=65536 THEN body END "
                "FROM daily_states WHERE day_id=?",
                (key,),
            ).fetchone()
            if row and row[0]:
                body = {k: v for k, v in json.loads(row[0]).items() if k in PUBLIC}
                rev = db.execute(
                    "SELECT revision FROM daily_revisions WHERE day_id=?", (key,)
                ).fetchone()
                if rev:
                    body["revision"] = max(body.get("revision", 0), rev[0])
                return body
        return dict(self._index(db, key, fingerprint)[0])

    @staticmethod
    def _values(db, runs, offset, limit):
        for run in runs:
            if offset >= run.count:
                offset -= run.count
                continue
            block = offset // STRIDE
            with db.blobopen(run.source[0], "body", run.source[1], readonly=True) as blob:
                blob.seek(run.offsets[block])
                reader = SnapshotJSON(blob, max_value_bytes=65536)
                position = block * STRIDE
                while position < run.count and limit:
                    item = reader.value()
                    if position >= offset:
                        yield item
                        limit -= 1
                    position += 1
                    if position < run.count and limit:
                        reader.take(",")
            offset = 0
            if not limit:
                return

    def read(self, key, *, summary=False):
        with self.connection() as db:
            return self._read(db, key, summary=summary)

    def _read(self, db, key, *, summary=False):
        fingerprint = self._snapshot(db, key)
        if summary:
            return self._state(db, key, fingerprint)
        state, arrays = self._index(db, key, fingerprint)
        result = dict(state)
        size = len(json.dumps(result).encode()) + 128
        for name in sorted(ARRAYS):
            result[name] = []
            for item in self._values(db, arrays[name], 0, sum(r.count for r in arrays[name])):
                size += len(json.dumps(item).encode()) + 2
                if size > self.response_bytes:
                    raise too_large()
                result[name].append(item)
        return result

    def listing(self, camera, filters, limit, after, *, summary=False):
        clauses, args = ["camera=?"], [camera]
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
        with self.connection() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM daily_recordings WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY date DESC LIMIT ?",
                    (*args, limit + 1),
                )
            ]
            items, size = [], 1024
            for key in ids[:limit]:
                item = self._read(db, key, summary=summary)
                size += len(json.dumps(item).encode()) + 2
                if size > self.response_bytes:
                    raise too_large()
                items.append(item)
            return items, len(ids) > limit

    def page(self, key, field, offset, limit, *, fingerprint=None):
        if field not in ARRAYS or type(offset) is not int or offset < 0 or not 1 <= limit <= 500:
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid timeline page.")
        with self.connection() as db:
            current = self._snapshot(db, key)
            if fingerprint and fingerprint != current:
                raise ApiError(
                    409, "DAILY_SNAPSHOT_CHANGED", "Recording changed; restart from summary."
                )
            state, arrays = self._index(db, key, current)
            total = sum(run.count for run in arrays[field])
            if offset > total:
                raise ApiError(400, "INVALID_CURSOR", "Invalid page offset.")
            items, size = [], 1024
            for item in self._values(db, arrays[field], offset, limit):
                size += len(json.dumps(item).encode()) + 2
                if size > self.response_bytes:
                    if not items:
                        raise too_large()
                    break
                items.append(item)
            end = offset + len(items)
            return {
                "recording_id": key,
                "revision": state.get("revision", 0),
                "snapshot": current,
                "items": items,
                "total": total,
                "next_offset": end if end < total else None,
            }

    def snapshot_id(self, key):
        with self.connection() as db:
            return self._snapshot(db, key)

    def retention_samples(self, cameras):
        samples = {}
        with self.connection() as db:
            for camera in cameras:
                ids = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM daily_recordings WHERE camera=? AND status='closed' "
                        "ORDER BY date DESC LIMIT 8",
                        (camera,),
                    )
                ]
                for key in ids:
                    state = self._state(db, key, self._snapshot(db, key))
                    if state.get("archive_state") != "mp4" or state.get("export_state") != "ready":
                        continue
                    seconds = state.get("recorded_seconds", 0)
                    if (
                        not isinstance(seconds, int | float)
                        or not 0.9 * 86400 <= seconds <= 1.1 * 86400
                    ):
                        continue
                    # The stored key is a filename, never an arbitrary path.
                    import uuid

                    uuid.UUID(key)
                    path = self.path.parent / "daily-exports" / (key + ".mp4")
                    try:
                        actual = path.stat().st_size
                    except OSError:
                        continue
                    if actual == state.get("size_bytes"):
                        samples[camera] = {
                            "recording_id": key,
                            "size_bytes": actual,
                            "recorded_seconds": seconds,
                        }
                        break
        return samples
