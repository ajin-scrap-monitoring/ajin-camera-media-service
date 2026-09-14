from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            for k, v in record.extra.items():
                if "token" in k.lower() or "secret" in k.lower() or "authorization" in k.lower():
                    log_obj[k] = "***MASKED***"
                else:
                    log_obj[k] = v
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)


def configure_logging(log_format: str = "json") -> None:
    root_logger = logging.getLogger()
    if log_format == "json":
        for handler in root_logger.handlers:
            handler.setFormatter(JsonLogFormatter())


@dataclass(slots=True)
class IngestTelemetryTracker:
    active_ingest_connections: int = 0
    total_frames_received: int = 0
    total_bytes_received: int = 0
    rejected_frames_count: int = 0
    active_delivery_sessions: int = 0

    def record_connection_start(self) -> None:
        self.active_ingest_connections += 1

    def record_connection_end(self) -> None:
        self.active_ingest_connections = max(0, self.active_ingest_connections - 1)

    def record_frame_ingested(self, bytes_size: int) -> None:
        self.total_frames_received += 1
        self.total_bytes_received += bytes_size

    def record_frame_rejected(self) -> None:
        self.rejected_frames_count += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "active_ingest_connections": self.active_ingest_connections,
            "total_frames_received": self.total_frames_received,
            "total_bytes_received": self.total_bytes_received,
            "rejected_frames_count": self.rejected_frames_count,
            "active_delivery_sessions": self.active_delivery_sessions,
        }


class StatsLoggerTask:
    def __init__(
        self,
        telemetry: IngestTelemetryTracker,
        interval_seconds: int = 60,
        logger: logging.Logger | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.interval_seconds = interval_seconds
        self.logger = logger or logging.getLogger("camera_media_service.telemetry")
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                stats = self.telemetry.snapshot()
                self.logger.info(f"Ingest Telemetry Stats: {json.dumps(stats)}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(f"Telemetry loop error: {exc}")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
