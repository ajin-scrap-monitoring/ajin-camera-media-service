"""Bounded, optional daily metadata work outside foreground recording progress."""

import asyncio
import copy
import logging
import time
from pathlib import Path

from camera_media_service.background_media import BackgroundMedia
from camera_media_service.media_files import _settle_worker, storage_io
from camera_media_service.persisted_media_info import file_identity, persist_generated_info

LOG = logging.getLogger(__name__)


class DailyMetadataWriter:
    def __init__(self, recording, *, max_pending=64, clock=None):
        self.recording = recording
        self.clock = clock or time.time
        self.queue = asyncio.Queue(max_pending)
        self.keys = set()
        self.task = None
        self.closed = False
        recording.optional_metadata_workers.add(self)

    def offer(self, path, metadata):
        if self.closed or not metadata.get("_file_identity"):
            return False
        if metadata.get("media_info", {}).get("status") != "ready":
            return False
        key = (Path(path), tuple(metadata["_file_identity"]))
        if key in self.keys:
            return True
        try:
            self.queue.put_nowait((key, copy.deepcopy(metadata)))
        except asyncio.QueueFull:
            self.recording.diagnostics.count("metadata_cache_full")
            return False
        self.keys.add(key)
        self.recording.diagnostics.gauge("metadata_cache_pending", self.queue.qsize())
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        return True

    def _current(self, path, identity):
        self.recording.storage_guard()
        if not path.resolve().is_relative_to(self.recording.root.resolve()):
            return False
        try:
            return file_identity(path.stat()) == identity
        except FileNotFoundError:
            return False

    async def _run(self):
        while True:
            key, metadata = await self.queue.get()
            try:
                await BackgroundMedia(self.recording, clock=self.clock).checkpoint()
                path, identity = key
                if not await storage_io(self._current, path, identity):
                    self.recording.diagnostics.count("metadata_cache_stale")
                    continue
                if not await persist_generated_info(self.recording, path, metadata):
                    self.recording.diagnostics.count("metadata_cache_unavailable")
            except Exception:
                self.recording.diagnostics.count("metadata_cache_unavailable")
                LOG.warning("Optional daily metadata deferred to on-demand probing", exc_info=True)
            finally:
                self.keys.discard(key)
                self.queue.task_done()
                self.recording.diagnostics.gauge("metadata_cache_pending", self.queue.qsize())

    async def join(self):
        """Explicit cache barrier for verification, never called by recording ticks."""
        await self.queue.join()

    def stop(self):
        if self.closed:
            return
        self.closed = True
        if self.task is not None:
            self.task.cancel()
        while not self.queue.empty():
            key, _ = self.queue.get_nowait()
            self.keys.discard(key)
            self.queue.task_done()
            self.recording.diagnostics.count("metadata_cache_shutdown_skipped")
        self.recording.diagnostics.gauge("metadata_cache_pending", 0)

    async def close(self):
        self.stop()
        if self.task is not None:
            # persist_generated_info/storage_io drain an owned in-flight worker
            # before the catalogue and storage guard can be torn down.
            await _settle_worker(self.task)
        self.recording.optional_metadata_workers.discard(self)
