from __future__ import annotations

import asyncio
import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from camera_media_service.pipeline import FrameEnvelope, LiveFrameHub


class StorageBackend(Protocol):
    async def write_frame(self, envelope: FrameEnvelope) -> str: ...


@dataclass(frozen=True, slots=True)
class FrameMeta:
    camera_id: str
    generation: int
    received_at: float
    path: str
    bytes_size: int


class RingBufferIndex:
    def __init__(self) -> None:
        self._index: dict[str, list[FrameMeta]] = {}

    def add(self, meta: FrameMeta) -> None:
        if meta.camera_id not in self._index:
            self._index[meta.camera_id] = []
        self._index[meta.camera_id].append(meta)

    def query(
        self,
        camera_id: str,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> list[FrameMeta]:
        records = self._index.get(camera_id, [])
        result: list[FrameMeta] = []
        for rec in records:
            if start_time is not None and rec.received_at < start_time:
                continue
            if end_time is not None and rec.received_at > end_time:
                continue
            result.append(rec)
        return result

    def count(self, camera_id: str) -> int:
        return len(self._index.get(camera_id, []))


class LocalStorageBackend:
    def __init__(self, media_root: str) -> None:
        self.media_root = Path(media_root)

    def _sync_write(self, envelope: FrameEnvelope) -> str:
        dt = datetime.datetime.fromtimestamp(envelope.received_at, tz=datetime.UTC)
        date_str = dt.strftime("%Y%m%d")
        hour_str = dt.strftime("%H")
        ts_ms = int(envelope.received_at * 1000)

        cam_dir = self.media_root / envelope.camera_id / date_str / hour_str
        cam_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{envelope.generation:08d}_{ts_ms}.jpg"
        file_path = cam_dir / filename

        # Write file atomically or directly
        tmp_path = cam_dir / f".tmp_{filename}"
        with open(tmp_path, "wb") as f:
            f.write(envelope.jpeg)
        os.replace(tmp_path, file_path)

        return str(file_path.resolve())

    async def write_frame(self, envelope: FrameEnvelope) -> str:
        return await asyncio.to_thread(self._sync_write, envelope)


class SegmentWriter:
    def __init__(
        self,
        hub: LiveFrameHub,
        backend: StorageBackend,
        index: RingBufferIndex,
        maxsize: int = 100,
    ) -> None:
        self.hub = hub
        self.backend = backend
        self.index = index
        self.maxsize = maxsize
        self._queue: asyncio.Queue[FrameEnvelope] | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._queue = self.hub.subscribe(maxsize=self.maxsize)
        self._task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        assert self._queue is not None
        while self._running:
            try:
                envelope = await self._queue.get()
                path = await self.backend.write_frame(envelope)
                meta = FrameMeta(
                    camera_id=envelope.camera_id,
                    generation=envelope.generation,
                    received_at=envelope.received_at,
                    path=path,
                    bytes_size=len(envelope.jpeg),
                )
                self.index.add(meta)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception:
                # Log or swallow error to protect ingest worker
                pass

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._queue is not None:
            self.hub.unsubscribe(self._queue)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
