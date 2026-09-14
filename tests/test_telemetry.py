from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from camera_media_service.telemetry import (
    IngestTelemetryTracker,
    JsonLogFormatter,
    StatsLoggerTask,
)


def test_json_log_formatter_masks_secrets() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Handshake auth",
        args=(),
        exc_info=None,
    )
    record.extra = {"camera_id": "cam-1", "authorization_token": "secret-123"}

    output = formatter.format(record)
    assert '"camera_id": "cam-1"' in output
    assert '"authorization_token": "***MASKED***"' in output
    assert '"secret-123"' not in output


def test_ingest_telemetry_tracker_records_metrics() -> None:
    tracker = IngestTelemetryTracker()

    tracker.record_connection_start()
    tracker.record_frame_ingested(1024)
    tracker.record_frame_rejected()

    snapshot = tracker.snapshot()
    assert snapshot["active_ingest_connections"] == 1
    assert snapshot["total_frames_received"] == 1
    assert snapshot["total_bytes_received"] == 1024
    assert snapshot["rejected_frames_count"] == 1

    tracker.record_connection_end()
    assert tracker.snapshot()["active_ingest_connections"] == 0


@pytest.mark.asyncio
async def test_stats_logger_task_runs_periodically() -> None:
    tracker = IngestTelemetryTracker()
    mock_logger = MagicMock()

    task = StatsLoggerTask(telemetry=tracker, interval_seconds=0.05, logger=mock_logger)
    task.start()

    await asyncio.sleep(0.12)
    await task.stop()

    assert mock_logger.info.call_count >= 1
