"""Run passive WAL checkpoints on a separate connection without the ingest lock."""

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

from camera_media_service.media_files import storage_io

LOG = logging.getLogger(__name__)


class CatalogCheckpointer:
    def __init__(self, path, guard, diagnostics, *, interval=0.5, min_bytes=256 * 1024):
        self.path, self.guard, self.diagnostics = Path(path), guard, diagnostics
        self.interval, self.min_bytes = interval, min_bytes
        self.connection = None
        self.task = None
        self.last_error_log = 0.0

    def _connect(self):
        self.guard()
        connection = sqlite3.connect(
            self.path, timeout=0, isolation_level=None, check_same_thread=False
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
        self.connection = connection

    async def start(self):
        if self.task is not None:
            return
        try:
            await storage_io(self._connect)
        except BaseException:
            if self.connection is not None:
                await storage_io(self.connection.close)
                self.connection = None
            raise
        self.task = asyncio.create_task(self._loop())

    def _wal_size(self):
        self.guard()
        return self.path.with_name(self.path.name + "-wal").stat().st_size

    def _checkpoint(self):
        self.guard()
        return self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()

    async def _loop(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                size = await storage_io(self._wal_size)
                if size < self.min_bytes:
                    continue
                with self.diagnostics.measure("catalog_checkpoint"):
                    row = await storage_io(self._checkpoint)
                self.diagnostics.gauge("checkpoint_pending_frames", max(0, row[1] - row[2]))
            except (OSError, sqlite3.Error):
                self.diagnostics.count("catalog_checkpoint_deferred")
                if time.monotonic() - self.last_error_log > 60:
                    LOG.warning("Catalogue checkpoint deferred", exc_info=True)
                    self.last_error_log = time.monotonic()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            # storage_io drains an in-flight checkpoint before cancellation finishes.
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.connection is not None:
            await storage_io(self.connection.close)
            self.connection = None
