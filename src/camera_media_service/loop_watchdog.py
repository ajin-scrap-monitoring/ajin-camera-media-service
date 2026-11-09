"""One bounded stack witness when the event-loop heartbeat stops advancing."""

import asyncio
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from camera_media_service.catalog_pins import finish_cleanup


class LoopWatchdog:
    def __init__(self, *, threshold=0.75, interval=0.05):
        self.threshold, self.interval = threshold, interval
        self.target_thread = threading.get_ident()
        self.last_heartbeat = time.monotonic()
        self.pending = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._watch, name="media-loop-watch", daemon=True)
            self.thread.start()

    def heartbeat(self):
        with self.lock:
            sample, self.pending = self.pending, None
            self.last_heartbeat = time.monotonic()
            return sample

    def _watch(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                age = time.monotonic() - self.last_heartbeat
                if age < self.threshold or self.pending is not None:
                    continue
                frame = sys._current_frames().get(self.target_thread)
                frames = []
                try:
                    while frame is not None and len(frames) < 12:
                        frames.append(
                            {
                                "file": Path(frame.f_code.co_filename).name,
                                "function": frame.f_code.co_name,
                                "line": frame.f_lineno,
                            }
                        )
                        frame = frame.f_back
                finally:
                    del frame  # Never retain frames, locals, arguments or JPEG data.
                self.pending = {
                    "observed_at": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "heartbeat_age_seconds": age,
                    "frames": frames,
                }

    async def close(self):
        self.stopped.set()
        if self.thread is not None:

            async def join():
                await asyncio.to_thread(self.thread.join)

            await finish_cleanup(join())
