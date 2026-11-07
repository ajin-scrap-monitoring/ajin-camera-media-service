"""Internal, identity-bound measured file metadata; never part of public JSON."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path

from camera_media_service.background_media import checkpoint
from camera_media_service.media_files import storage_io

LOG = logging.getLogger(__name__)
METADATA_DATABASE_NAME = "media-info.sqlite3"
_CONNECTIONS = OrderedDict()
_LEGACY_CONNECTIONS = OrderedDict()


def close_probe_database(db):
    """Release the owner's private connection before its main catalog closes."""
    for cache in (_CONNECTIONS, _LEGACY_CONNECTIONS):
        connection = cache.pop(db, None)
        if connection is not None:
            connection.close()


def _metadata_connection(db, root, *, create=False):
    connection = _CONNECTIONS.get(db)
    if connection is not None:
        _CONNECTIONS.move_to_end(db)
        return connection
    path = Path(root) / METADATA_DATABASE_NAME
    # Reads never create files or schema. All creation/writes occur under the
    # recording lock, on its owner thread, in an independent database.
    connection = sqlite3.connect(
        str(path) if create else path.as_uri() + "?mode=rw",
        uri=not create,
        timeout=0.05,
    )
    _CONNECTIONS[db] = connection
    while len(_CONNECTIONS) > 16:
        _, unused = _CONNECTIONS.popitem(last=False)
        unused.close()
    return connection


def _legacy_connection(db, root):
    # A FULL commit may own the writer connection's mutex on a disk worker.
    # Metadata must never wait on that mutex from the event loop. WAL readers
    # use their own read-only connection and cannot commit pending frame writes.
    connection = _LEGACY_CONNECTIONS.get(db)
    if connection is None:
        path = Path(root) / "catalog.sqlite3"
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
        _LEGACY_CONNECTIONS[db] = connection
        while len(_LEGACY_CONNECTIONS) > 16:
            _, unused = _LEGACY_CONNECTIONS.popitem(last=False)
            unused.close()
    _LEGACY_CONNECTIONS.move_to_end(db)
    return connection


def _lookup(db, root, query, key, identity):
    try:
        connection = _metadata_connection(db, root)
        row = connection.execute(query, (key,)).fetchone()
        if row and tuple(json.loads(row[0])) == tuple(identity):
            return json.loads(row[1])
    except (sqlite3.Error, ValueError, TypeError):
        pass
    # Earlier releases kept measurements in the primary catalog. Reuse them
    # lazily only when their identity still agrees, without migrating/scanning.
    try:
        row = _legacy_connection(db, root).execute(query, (key,)).fetchone()
        if row and tuple(json.loads(row[0])) == tuple(identity):
            return json.loads(row[1])
    except (sqlite3.Error, ValueError, TypeError):
        pass
    return None


def file_identity(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def ensure_probe_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS measured_media_files("
        "path TEXT PRIMARY KEY, identity TEXT NOT NULL, media_info TEXT NOT NULL)"
    )


def load_measured_info(db, root, path, identity):
    key = Path(path).relative_to(root).as_posix()
    return _lookup(
        db, root, "SELECT identity,media_info FROM measured_media_files WHERE path=?", key, identity
    )


def _optional_write(db, root, operation, *, connection=None):
    """A private ROLLBACK must never reach the caller's primary transaction."""
    try:
        connection = connection or _metadata_connection(db, root, create=True)
        with connection:
            connection.execute("BEGIN")
            operation(connection)
        return True
    except sqlite3.Error:
        LOG.warning("Optional measured metadata could not be saved", exc_info=True)
        return False


def record_generated_info(db, root, path, metadata):
    """Best-effort private transaction; caller holds the recording lock.

    Generation probes attest the stable file identity including the final rename.
    Legacy JSON without that evidence must be measured on demand. Failure never
    prevents the caller from persisting its primary ready state.
    """
    info, identity = metadata.get("media_info"), metadata.get("_file_identity")
    if not identity or not info or info["status"] != "ready":
        return False
    return _optional_write(
        db, root, lambda connection: _save_generated(connection, root, path, metadata)
    )


def _save_generated(connection, root, path, metadata):
    info, identity = metadata.get("media_info"), metadata.get("_file_identity")
    if not identity or not info or info["status"] != "ready":
        return
    key = Path(path).relative_to(root).as_posix()
    ensure_probe_table(connection)
    connection.execute(
        "INSERT OR REPLACE INTO measured_media_files VALUES(?,?,?)",
        (key, json.dumps(identity), json.dumps(info)),
    )


async def persist_generated_info(recording, path, metadata, *, archive_segments=None):
    """Drain optional FULL commits on a separately owned worker connection.

    Call outside the primary recording lock. No owner-thread cache connection is
    shared with the worker; failures cannot roll back primary video state.
    """
    if (
        not metadata.get("_file_identity")
        or metadata.get("media_info", {}).get("status") != "ready"
    ):
        return False

    def persist():
        connection = None
        try:
            connection = sqlite3.connect(recording.root / METADATA_DATABASE_NAME, timeout=0.05)

            def save(db):
                _save_generated(db, recording.root, path, metadata)
                if archive_segments is not None:
                    _save_verified(db, recording.root, path, metadata, archive_segments)

            return _optional_write(None, recording.root, save, connection=connection)
        except (sqlite3.Error, OSError):
            LOG.warning("Optional measured metadata could not be saved", exc_info=True)
            return False
        finally:
            if connection is not None:
                connection.close()

    diagnostics = getattr(recording, "diagnostics", None)
    with diagnostics.measure("metadata_commit") if diagnostics else nullcontext():
        return await storage_io(persist)


def load_verified_archive(db, root, path, identity):
    key = Path(path).relative_to(root).as_posix()
    return _lookup(
        db,
        root,
        "SELECT identity,segments FROM verified_media_archives WHERE path=?",
        key,
        identity,
    )


def record_verified_archive(db, root, path, metadata, segments):
    """Bind verified source measurements to this exact verified master, privately."""
    identity = metadata.get("_file_identity")
    if not identity:
        return False
    return _optional_write(
        db, root, lambda connection: _save_verified(connection, root, path, metadata, segments)
    )


def _save_verified(connection, root, path, metadata, segments):
    identity = metadata.get("_file_identity")
    if not identity:
        return
    key = Path(path).relative_to(root).as_posix()
    connection.execute(
        "CREATE TABLE IF NOT EXISTS verified_media_archives("
        "path TEXT PRIMARY KEY, identity TEXT NOT NULL, segments TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT OR REPLACE INTO verified_media_archives VALUES(?,?,?)",
        (key, json.dumps(identity), json.dumps(segments)),
    )


async def source_measurements(recording, segments):
    """Snapshot only identity-attested source info during actual archive generation."""
    measured = {}
    for index, segment in enumerate(segments):
        if index % 64 == 0:
            await checkpoint()
        path = recording.root / segment["path"]
        try:
            identity = file_identity(await asyncio.to_thread(path.stat))
            async with recording.lock:
                info = load_measured_info(recording.db, recording.root, path, identity)
            if info and file_identity(await asyncio.to_thread(path.stat)) == identity:
                measured[segment["segment_id"]] = {"identity": identity, "media_info": info}
        except OSError:
            continue
    return measured


async def publish_generated_info(metadata, temporary, final):
    """Publish the verified inode, checking the complete pre-rename identity."""

    def publish():
        before = file_identity(temporary.stat())
        if tuple(metadata["_file_identity"]) != before:
            raise RuntimeError("Media changed after verification")
        os.replace(temporary, final)
        after = file_identity(final.stat())
        # The writer's own rename may change ctime; content/inode must agree.
        if before[:4] != after[:4]:
            raise RuntimeError("Media changed while publishing")
        return after

    metadata["_file_identity"] = await storage_io(publish)
