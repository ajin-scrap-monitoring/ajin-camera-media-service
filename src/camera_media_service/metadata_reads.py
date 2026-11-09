"""Worker-owned read-only measurement connections and bulk identity checks."""

import json
import sqlite3

from camera_media_service.persisted_media_info import file_identity
from camera_media_service.recording import ApiError


class ProbeReader:
    def __init__(self, root):
        self.root = root
        self.connections = []

    def __enter__(self):
        for name in ("media-info.sqlite3", "catalog.sqlite3"):
            try:
                db = sqlite3.connect(
                    (self.root / name).as_uri() + "?mode=ro", uri=True, timeout=0.05
                )
                db.execute("PRAGMA query_only=ON")
                self.connections.append(db)
            except sqlite3.Error:
                continue
        return self

    def __exit__(self, *args):
        for db in self.connections:
            db.close()

    def lookup(self, path, identity, *, archive=False):
        key = path.relative_to(self.root).as_posix()
        table, column = (
            ("verified_media_archives", "segments")
            if archive
            else ("measured_media_files", "media_info")
        )
        for db in self.connections:
            try:
                row = db.execute(
                    f"SELECT identity,{column} FROM {table} WHERE path=?", (key,)
                ).fetchone()
                if row and tuple(json.loads(row[0])) == tuple(identity):
                    return json.loads(row[1])
            except (sqlite3.Error, ValueError, TypeError):
                continue
        return None


def read_measurement(root, path, identity, guard=None, *, archive=False):
    if guard:
        guard()
    with ProbeReader(root) as reader:
        return reader.lookup(path, identity, archive=archive)


def read_segment_measurements(root, parts, guard=None):
    if guard:
        guard()
    directory = (root / "daily").resolve()
    results = []
    with ProbeReader(root) as reader:
        for part in parts:
            path = (root / part["path"]).resolve()
            if not path.is_relative_to(directory):
                raise ApiError(503, "STORAGE_UNAVAILABLE", "Media path is unavailable.")
            info, identity = None, None
            try:
                identity = file_identity(path.stat())
                info = reader.lookup(path, identity)
                if info is not None and file_identity(path.stat()) != identity:
                    info = None
            except OSError:
                pass
            results.append((path, identity, info))
    if guard:
        guard()
    return results
