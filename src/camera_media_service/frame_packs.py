"""Immutable JPEG bundles, byte-range descriptors and physical-byte accounting."""

import os
import re

from camera_media_service.daily_models import StoredFrame

PACK_NAME = re.compile(r"[0-9a-f]{32}\.jpack\Z")
TEMP_NAME = re.compile(r"[0-9a-f]{32}\.tmp\.pack\Z")


def input_name(frame, *, event=False):
    if frame.pack is None:
        return ("../buffer/" if event else "") + frame.path
    if not PACK_NAME.fullmatch(frame.pack) or frame.offset < 0 or frame.size_bytes <= 0:
        raise OSError("Invalid packed frame range")
    return (
        f"subfile,,start,{frame.offset},end,{frame.offset + frame.size_bytes},,:file:{frame.pack}"
    )


def validate_inputs(buffer, frames):
    lengths = {}
    for frame in frames:
        if frame.pack is None:
            continue
        input_name(frame)
        if frame.pack not in lengths:
            path = buffer / frame.pack
            if path.is_symlink():
                raise OSError("Invalid packed frame source")
            lengths[frame.pack] = path.stat().st_size
        if frame.offset + frame.size_bytes > lengths[frame.pack]:
            raise OSError("Truncated packed frame source")


class FramePacks:
    def __init__(self, db, buffer, guard):
        self.db, self.buffer, self.guard = db, buffer, guard
        db.executescript("""
            CREATE TABLE IF NOT EXISTS frame_packs(pack TEXT PRIMARY KEY,size INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS frame_ranges(
                path TEXT PRIMARY KEY,pack TEXT NOT NULL,offset_bytes INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS frame_range_pack ON frame_ranges(pack);
        """)

    def write(self, name, frames):
        self.guard()
        if not PACK_NAME.fullmatch(name):
            raise OSError("Invalid pack name")
        temporary = self.buffer / (name[:-6] + ".tmp.pack")
        destination = self.buffer / name
        if destination.exists():
            raise FileExistsError(destination)
        rows, ranges, position = [], [], 0
        for index, frame in enumerate(frames):
            key = name[:-6] + f"-{index:04d}.jpg"
            rows.append((key, frame.camera_id, frame.received_at, len(frame.jpeg)))
            ranges.append((key, name, position))
            position += len(frame.jpeg)
        with temporary.open("xb") as file:
            # One bounded contiguous write, no JPEG decode or per-frame files.
            file.write(b"".join(f.jpeg for f in frames))
            file.flush()
            os.fsync(file.fileno())
        self.guard()
        os.replace(temporary, destination)
        if os.name != "nt":
            directory = os.open(self.buffer, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return rows, ranges, position

    def insert(self, name, prepared):
        rows, ranges, size = prepared
        self.db.execute("INSERT INTO frame_packs VALUES (?,?)", (name, size))
        self.db.executemany("INSERT INTO frames VALUES (?,?,?,?)", rows)
        self.db.executemany("INSERT INTO frame_ranges VALUES (?,?,?)", ranges)

    def discard(self, name):
        self.guard()
        if not PACK_NAME.fullmatch(name):
            raise OSError("Invalid pack cleanup name")
        (self.buffer / name).unlink(missing_ok=True)
        (self.buffer / (name[:-6] + ".tmp.pack")).unlink(missing_ok=True)

    def bytes(self):
        return self.db.execute("""
            SELECT (SELECT coalesce(sum(size),0) FROM frame_packs) +
            (SELECT coalesce(sum(f.size),0) FROM frames f WHERE NOT EXISTS
                (SELECT 1 FROM frame_ranges r WHERE r.path=f.path))
        """).fetchone()[0]

    def descriptors(self, rows):
        return [
            StoredFrame(r["path"], r["timestamp"], r["size"], r["pack"], r["offset_bytes"] or 0)
            for r in rows
        ]

    def read(self, frame):
        self.guard()
        validate_inputs(self.buffer, [frame])
        with (self.buffer / (frame.pack or frame.path)).open("rb") as file:
            file.seek(frame.offset)
            data = file.read(frame.size_bytes)
        if len(data) != frame.size_bytes:
            raise OSError("Truncated frame source")
        return data

    def empty(self):
        return [
            r[0]
            for r in self.db.execute("""
            SELECT p.pack FROM frame_packs p WHERE NOT EXISTS
                (SELECT 1 FROM frame_ranges r WHERE r.pack=p.pack)
        """)
        ]

    def reconcile(self):
        self.guard()
        with self.db:
            self.db.execute(
                "DELETE FROM frame_ranges WHERE NOT EXISTS "
                "(SELECT 1 FROM frames f WHERE f.path=frame_ranges.path)"
            )
            missing = self.db.execute("""
                SELECT r.pack,max(r.offset_bytes+f.size) FROM frame_ranges r
                JOIN frames f ON f.path=r.path WHERE NOT EXISTS
                    (SELECT 1 FROM frame_packs p WHERE p.pack=r.pack) GROUP BY r.pack
            """).fetchall()
            for name, expected in missing:
                if not PACK_NAME.fullmatch(name):
                    raise OSError("Invalid referenced frame pack")
                path = self.buffer / name
                if path.is_symlink():
                    raise OSError("Invalid referenced frame pack")
                size = max(expected, path.stat().st_size) if path.exists() else expected
                self.db.execute("INSERT INTO frame_packs VALUES (?,?)", (name, size))
        referenced = {r[0] for r in self.db.execute("SELECT pack FROM frame_packs")}
        for path in self.buffer.iterdir():
            if TEMP_NAME.fullmatch(path.name) or (
                PACK_NAME.fullmatch(path.name) and path.name not in referenced
            ):
                self.guard()
                path.unlink(missing_ok=True)
