"""Bounded internal timing and loss attribution; no public API schema changes."""

import asyncio
import json
import logging
import time
from contextlib import contextmanager

LOG = logging.getLogger(__name__)


class RecordingDiagnostics:
    def __init__(self):
        self.stages = {}
        self.loss_counts = {}
        self.gauges = {}
        self.last_report = time.monotonic()

    @staticmethod
    def _key(mapping, key):
        return key if key in mapping or len(mapping) < 64 else "other"

    def count(self, cause):
        cause = self._key(self.loss_counts, cause)
        self.loss_counts[cause] = self.loss_counts.get(cause, 0) + 1

    def gauge(self, name, value):
        self.gauges[self._key(self.gauges, name)] = value

    def peak(self, name, value):
        self.gauge(name, max(self.gauges.get(name, 0), value))

    def observe(self, stage, seconds):
        stage = self._key(self.stages, stage)
        row = self.stages.setdefault(stage, {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0})
        row["count"] += 1
        row["total_seconds"] += seconds
        row["max_seconds"] = max(row["max_seconds"], seconds)

    @contextmanager
    def measure(self, stage):
        start = time.monotonic()
        try:
            yield
        finally:
            self.observe(stage, time.monotonic() - start)

    def snapshot(self):
        return {
            "stages": {k: dict(v) for k, v in self.stages.items()},
            "loss_counts": dict(self.loss_counts),
            "gauges": dict(self.gauges),
        }

    def report_if_due(self):
        now = time.monotonic()
        if now - self.last_report >= 60:
            LOG.info("Recording diagnostics: %s", json.dumps(self.snapshot(), sort_keys=True))
            self.stages.clear()
            self.loss_counts.clear()
            for key in list(self.gauges):
                if key.endswith("_peak"):
                    self.gauges.pop(key)
            self.last_report = now


class RecordingLock(asyncio.Lock):
    def __init__(self, diagnostics):
        super().__init__()
        self.diagnostics = diagnostics
        self.acquired_at = None

    async def acquire(self):
        start = time.monotonic()
        result = await super().acquire()
        self.acquired_at = time.monotonic()
        self.diagnostics.observe("catalog_lock_wait", self.acquired_at - start)
        return result

    def release(self):
        if self.acquired_at is not None:
            self.diagnostics.observe("catalog_lock_hold", time.monotonic() - self.acquired_at)
            self.acquired_at = None
        super().release()
