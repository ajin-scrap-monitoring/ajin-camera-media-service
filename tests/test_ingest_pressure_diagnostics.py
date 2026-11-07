import asyncio

import pytest

from camera_media_service.pipeline import LiveFrameHub
from camera_media_service.recording_diagnostics import RecordingDiagnostics
from camera_media_service.store import LatestFrameStore


@pytest.mark.parametrize(
    "maxsize,max_bytes,payload,expected",
    [
        (1, 100, b"123", "ingest_queue_frame_limit"),
        (10, 5, b"123", "ingest_queue_byte_limit"),
        (10, 2, b"123", "ingest_frame_oversize"),
    ],
)
def test_actual_pressure_limit_is_distinguished(maxsize, max_bytes, payload, expected):
    hub = LiveFrameHub(LatestFrameStore())
    hub.recording_diagnostics = stats = RecordingDiagnostics()
    hub.subscribe(maxsize, recording=True, max_bytes=max_bytes)
    hub.publish("cam", payload)
    hub.publish("cam", payload)
    assert stats.loss_counts[expected] == stats.loss_counts["ingest_queue_full"]
    assert stats.gauges.get("ingest_queue_frames_peak", 0) <= maxsize
    assert stats.gauges.get("ingest_queue_bytes_peak", 0) <= max_bytes


@pytest.mark.asyncio
async def test_loop_delay_is_observed_separately_from_storage_wait(tmp_path):
    import time

    from test_recording import make_service

    service = make_service(tmp_path)
    await service.start()
    try:
        await asyncio.sleep(0.02)
        time.sleep(0.35)  # Intentional CPU/loop pause, not a storage await.
        await asyncio.sleep(0.3)
        assert service.diagnostics.stages["event_loop_lag"]["max_seconds"] > 0.08
    finally:
        await service.close()
