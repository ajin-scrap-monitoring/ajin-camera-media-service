"""Foreground-aware control for owned, optional file-processing children."""

from __future__ import annotations

import asyncio
import contextvars
import os
import signal
import time
from contextlib import asynccontextmanager

CURRENT = contextvars.ContextVar("background_media", default=None)


class BackgroundMedia:
    def __init__(self, recording, *, clock=time.time, readrate=8):
        self.recording, self.clock, self.readrate = recording, clock, readrate
        self.watchers = set()

    def pressured(self, *, recovering=False):
        queue = self.recording.queue
        if queue is None:
            return False
        frames = 15 if recovering else 60
        size = 1024**2 if recovering else 8 * 1024**2
        lag = 15 if recovering else 20
        return (
            queue.qsize() > frames
            or getattr(queue, "queued_bytes", 0) > size
            or self.recording.storage_error
            or any(
                self.clock() - cursor > lag
                for cursor in self.recording.daily_frame_watermarks.values()
            )
        )

    async def checkpoint(self):
        if self.pressured():
            self.recording.diagnostics.count("background_deferred")
            while self.pressured(recovering=True):
                await asyncio.sleep(0.1)

    @asynccontextmanager
    async def scope(self, *, admit=True):
        token = CURRENT.set(self)
        try:
            if admit:
                await self.checkpoint()
            yield
        finally:
            # Encoding's finally owns kill/reap. Never leave a live child stopped
            # if scope cancellation arrives while its monitor is paused.
            tasks = list(self.watchers)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            CURRENT.reset(token)

    def watch(self, process):
        if os.name == "nt":
            return  # File-input readrate and phase admission still apply here.
        task = asyncio.create_task(self._watch(process))
        self.watchers.add(task)
        task.add_done_callback(self.watchers.discard)

    async def _watch(self, process):
        paused = False
        recovered_since = None
        try:
            while process.returncode is None:
                if not paused and self.pressured():
                    process.send_signal(signal.SIGSTOP)
                    paused = True
                    self.recording.diagnostics.count("background_process_paused")
                    self.recording.diagnostics.gauge("background_process_paused", 1)
                elif paused:
                    if self.pressured(recovering=True):
                        recovered_since = None
                    elif recovered_since is None:
                        recovered_since = time.monotonic()
                    elif time.monotonic() - recovered_since >= 0.3:
                        process.send_signal(signal.SIGCONT)
                        paused = False
                        recovered_since = None
                        self.recording.diagnostics.gauge("background_process_paused", 0)
                await asyncio.sleep(0.05)
        except ProcessLookupError:
            pass
        finally:
            if paused and process.returncode is None:
                try:
                    process.send_signal(signal.SIGCONT)
                except ProcessLookupError:
                    pass
            self.recording.diagnostics.gauge("background_process_paused", 0)


def file_input_options():
    control = CURRENT.get()
    return ["-readrate", str(control.readrate)] if control is not None else []


async def checkpoint():
    control = CURRENT.get()
    if control is not None:
        await control.checkpoint()
