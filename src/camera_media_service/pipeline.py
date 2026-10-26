from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from camera_media_service.store import LatestFrameStore


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    camera_id: str
    received_at: float
    generation: int
    jpeg: bytes


class RecordingQueue(asyncio.Queue[FrameEnvelope]):
    """A finite receipt barrier, independent of frames arriving after the snapshot."""

    def __init__(self, maxsize: int):
        super().__init__(maxsize)
        self._sequence = 0
        self._pending: dict[int, int] = {}
        self._receipt_times: dict[int, tuple[str, float]] = {}
        self._changed = asyncio.Event()

    def put_nowait(self, item: FrameEnvelope) -> None:
        super().put_nowait(item)
        self._sequence += 1
        self._pending[id(item)] = self._sequence
        self._receipt_times[id(item)] = (item.camera_id, item.received_at)

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
        period = max(0.001, min(1, received_at - previous.received_at)) if previous else 1 / 30
        self.latest[camera_id] = envelope

        for queue in list(self._subscribers):
            if queue.full():
                try:
                    dropped = queue.get_nowait()
                    if isinstance(queue, RecordingQueue):
                        queue.acknowledge(dropped)
                        if self.loss_observer is not None:
                            self.loss_observer(
                                dropped.camera_id,
                                dropped.received_at,
                                dropped.received_at + period,
                                "BUFFER_DROPPED",
                            )
                    else:
                        queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                pass

        return envelope

    def subscribe(
        self, maxsize: int = 100, *, recording: bool = False
    ) -> asyncio.Queue[FrameEnvelope]:
        queue = RecordingQueue(maxsize) if recording else asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[FrameEnvelope]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
