from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from camera_media_service.store import LatestFrameStore

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    camera_id: str
    received_at: float
    generation: int
    jpeg: bytes


class RecordingQueue(asyncio.Queue[FrameEnvelope]):
    """A finite receipt barrier, independent of frames arriving after the snapshot."""

    def __init__(self, maxsize: int, *, max_bytes: int | None = None):
        super().__init__(maxsize)
        self.max_bytes = max_bytes
        self.queued_bytes = 0
        self._sequence = 0
        self._pending: dict[int, int] = {}
        self._receipt_times: dict[int, tuple[str, float]] = {}
        self._changed = asyncio.Event()
        self._available = asyncio.Event()

    def put_nowait(self, item: FrameEnvelope) -> None:
        if not self.can_accept(item):
            raise asyncio.QueueFull
        super().put_nowait(item)
        self._sequence += 1
        self._pending[id(item)] = self._sequence
        self._receipt_times[id(item)] = (item.camera_id, item.received_at)

    def can_accept(self, item: FrameEnvelope) -> bool:
        return not self.full() and (
            self.max_bytes is None or self.queued_bytes + len(item.jpeg) <= self.max_bytes
        )

    def _put(self, item: FrameEnvelope) -> None:
        super()._put(item)
        self.queued_bytes += len(item.jpeg)
        self._available.set()

    def _get(self) -> FrameEnvelope:
        item = super()._get()
        self.queued_bytes -= len(item.jpeg)
        if not self._queue:
            self._available.clear()
        return item

    def next_size(self):
        return len(self._queue[0].jpeg) if self._queue else None

    async def wait_available(self):
        while self.empty():
            await self._available.wait()

    def acknowledge(self, item: FrameEnvelope) -> None:
        self._pending.pop(id(item))
        self._receipt_times.pop(id(item))
        self.task_done()
        self._changed.set()

    def pending_timestamps(self, camera_id: str) -> list[float]:
        """Include queued and writer-owned receipts until their durable acknowledgement."""
        return [at for camera, at in self._receipt_times.values() if camera == camera_id]

    async def wait_received(self) -> None:
        through = self._sequence
        while any(sequence <= through for sequence in self._pending.values()):
            self._changed.clear()
            await self._changed.wait()


class FrameValidator:
    def __init__(self, max_frame_bytes: int) -> None:
        self.max_frame_bytes = max_frame_bytes

    def is_jpeg(self, frame: bytes) -> bool:
        return len(frame) >= 4 and frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")

    def is_within_size(self, frame: bytes) -> bool:
        return len(frame) <= self.max_frame_bytes


class LiveFrameHub:
    def __init__(self, frame_store: LatestFrameStore) -> None:
        self.frame_store = frame_store
        self._subscribers: set[asyncio.Queue[FrameEnvelope]] = set()
        self.latest: dict[str, FrameEnvelope] = {}
        self.loss_observer: Callable[[str, float, float, str], None] | None = None
        self.connection_observer: Callable[[str, bool, float], None] | None = None

    def publish(self, camera_id: str, jpeg: bytes) -> FrameEnvelope:
        generation = self.frame_store.put(camera_id, jpeg)
        received_at = time.time()
        envelope = FrameEnvelope(
            camera_id=camera_id,
            received_at=received_at,
            generation=generation,
            jpeg=jpeg,
        )
        previous = self.latest.get(camera_id)
        diagnostics = getattr(self, "recording_diagnostics", None)
        if previous and received_at - previous.received_at > 1:
            delta = received_at - previous.received_at
            if diagnostics is not None:
                diagnostics.observe("receipt_time_gap", delta)
            LOG.warning(
                "JPEG receipt-time gap: camera=%s previous=%.6f current=%.6f seconds=%.3f",
                camera_id,
                previous.received_at,
                received_at,
                delta,
            )
        period = max(0.001, min(1, received_at - previous.received_at)) if previous else 1 / 30
        self.latest[camera_id] = envelope

        for queue in list(self._subscribers):
            if isinstance(queue, RecordingQueue):
                diagnostics = getattr(self, "recording_diagnostics", None)

                if queue.max_bytes is not None and len(envelope.jpeg) > queue.max_bytes:
                    self._recording_loss(envelope, period, "ingest_frame_oversize")
                    continue
                while not queue.can_accept(envelope):
                    cause = (
                        "ingest_queue_frame_limit" if queue.full() else "ingest_queue_byte_limit"
                    )
                    dropped = queue.get_nowait()
                    queue.acknowledge(dropped)
                    self._recording_loss(dropped, period, cause)
                queue.put_nowait(envelope)
                if diagnostics is not None:
                    diagnostics.gauge("ingest_queue_frames", queue.qsize())
                    diagnostics.gauge("ingest_queue_bytes", queue.queued_bytes)
                    diagnostics.peak("ingest_queue_frames_peak", queue.qsize())
                    diagnostics.peak("ingest_queue_bytes_peak", queue.queued_bytes)
                continue
            if queue.full():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                pass

        return envelope

    def _recording_loss(self, dropped: FrameEnvelope, period: float, cause: str) -> None:
        diagnostics = getattr(self, "recording_diagnostics", None)
        if diagnostics is not None:
            diagnostics.count("ingest_queue_full")
            diagnostics.count(cause)
        if self.loss_observer is not None:
            self.loss_observer(
                dropped.camera_id,
                dropped.received_at,
                dropped.received_at + period,
                "BUFFER_DROPPED",
            )

    def subscribe(
        self, maxsize: int = 100, *, recording: bool = False, max_bytes: int | None = None
    ) -> asyncio.Queue[FrameEnvelope]:
        queue = (
            RecordingQueue(maxsize, max_bytes=max_bytes)
            if recording
            else asyncio.Queue(maxsize=maxsize)
        )
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[FrameEnvelope]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
