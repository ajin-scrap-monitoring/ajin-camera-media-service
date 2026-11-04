"""Private optional catalogue, independent of frame/video transactions."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

from camera_media_service.media_files import storage_io


class ThumbnailStore:
    def __init__(self, root):
        self.path = Path(root) / "thumbnails.sqlite3"
        self.lock = asyncio.Lock()
        self.connection = None

    async def start(self):
        def start():
            if self.path.is_symlink():
                raise OSError("Invalid thumbnail catalogue")
            self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=0.1)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS thumbnails("
                "kind TEXT NOT NULL,id TEXT NOT NULL,state TEXT NOT NULL,"
                "body TEXT NOT NULL,PRIMARY KEY(kind,id))"
            )
            self.connection.commit()

        async with self.lock:
            await storage_io(start)

    async def get(self, kind, key):
        def get():
            row = self.connection.execute(
                "SELECT body FROM thumbnails WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            if not row:
                return None
            try:
                body = json.loads(row[0])
                identifier = "clip_id" if kind == "clip" else "recording_id"
                if (
                    not isinstance(body, dict)
                    or body.get(identifier) != key
                    or not isinstance(body.get("camera_id"), str)
                    or body.get("status")
                    not in ("ready", "pending", "failed", "unavailable", "unsupported")
                ):
                    raise ValueError()
                for field in ("_source_identity", "_image_identity"):
                    value = body.get(field)
                    if value is not None and (
                        not isinstance(value, list)
                        or len(value) != 5
                        or any(type(part) is not int for part in value)
                    ):
                        raise ValueError()
                if body["status"] == "ready":
                    expected = (
                        f"/api/v1/clips/{key}/thumbnail"
                        if kind == "clip"
                        else f"/api/v2/daily-recordings/{key}/thumbnail"
                    )
                    if (
                        body.get("content_path") != expected
                        or any(
                            type(body.get(field)) is not int or body[field] <= 0
                            for field in ("width", "height", "size_bytes")
                        )
                        or body["width"] > 640
                        or body["height"] > 360
                    ):
                        raise ValueError()
                return body
            except (ValueError, TypeError, KeyError) as exc:
                raise sqlite3.DatabaseError("Invalid optional thumbnail record") from exc

        async with self.lock:
            return await storage_io(get)

    async def put(self, kind, key, body):
        def put():
            with self.connection:
                self.connection.execute(
                    "INSERT OR REPLACE INTO thumbnails VALUES(?,?,?,?)",
                    (kind, key, body["status"], json.dumps(body)),
                )

        async with self.lock:
            await storage_io(put)

    @staticmethod
    def _keys(rows):
        try:
            for kind, key in rows:
                if kind not in ("clip", "day") or str(uuid.UUID(key)) != key:
                    raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise sqlite3.DatabaseError("Invalid optional thumbnail identifier") from exc
        return rows

    async def pending(self):
        async with self.lock:
            return await storage_io(
                lambda: self._keys(
                    self.connection.execute(
                        "SELECT kind,id FROM thumbnails WHERE state='pending' LIMIT 64"
                    ).fetchall()
                )
            )

    async def page(self, after=None):
        async with self.lock:
            return await storage_io(
                lambda: self._keys(
                    self.connection.execute(
                        "SELECT kind,id FROM thumbnails WHERE (kind,id)>(?,?) "
                        "ORDER BY kind,id LIMIT 32",
                        after or ("", ""),
                    ).fetchall()
                )
            )

    async def close(self):
        async with self.lock:
            if self.connection is not None:
                await storage_io(self.connection.close)
                self.connection = None
