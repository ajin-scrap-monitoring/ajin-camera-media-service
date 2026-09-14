from __future__ import annotations


class LatestFrameStore:
    def __init__(self) -> None:
        self._frames: dict[str, tuple[int, bytes]] = {}

    def put(self, camera_id: str, frame: bytes) -> int:
        current_generation = self._frames.get(camera_id, (0, b""))[0]
        generation = current_generation + 1
        copied_frame = memoryview(frame).tobytes()
        self._frames[camera_id] = (generation, copied_frame)
        return generation

    def get(self, camera_id: str) -> tuple[int, bytes] | None:
        current = self._frames.get(camera_id)
        if current is None:
            return None

        generation, frame = current
        return generation, memoryview(frame).tobytes()
