from __future__ import annotations

import asyncio
import time
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

    def publish(self, camera_id: str, jpeg: bytes) -> FrameEnvelope:
        generation = self.frame_store.put(camera_id, jpeg)
        received_at = time.time()
        envelope = FrameEnvelope(
            camera_id=camera_id,
            received_at=received_at,
            generation=generation,
            jpeg=jpeg,
        )

        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                pass

        return envelope

    def subscribe(self, maxsize: int = 100) -> asyncio.Queue[FrameEnvelope]:
        queue: asyncio.Queue[FrameEnvelope] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[FrameEnvelope]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
