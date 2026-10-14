import random
import time
from datetime import date

import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_gaps import build_gaps
from camera_media_service.daily_models import DailyWindow
from camera_media_service.recording import iso


def reference_gaps(spans, known):
    """The published rule: latest overlapping reason wins; unknown loss must exceed 1s."""
    result = []
    for a, b in spans:
        boundaries = sorted(
            {a, b, *[max(a, min(b, v)) for r in known for v in (r["start"], r["end"])]}
        )
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            reasons = [r["reason"] for r in known if r["start"] < right and r["end"] > left]
            if reasons or right - left > 1:
                reason = reasons[-1] if reasons else "UNKNOWN_CAPTURE_LOSS"
                if (
                    result
                    and result[-1]["reason_code"] == reason
                    and result[-1]["end_at"] == iso(left)
                ):
                    result[-1]["end_at"] = iso(right)
                else:
                    result.append(
                        {"start_at": iso(left), "end_at": iso(right), "reason_code": reason}
                    )
    return result


def test_loss_priority_and_missing_time_contract_are_preserved():
    rng = random.Random(20261015)
    for _ in range(100):
        boundaries = sorted(rng.sample(range(100), 20))
        spans = list(zip(boundaries[::2], boundaries[1::2], strict=False))
        known = []
        for _ in range(30):
            left, right = sorted(rng.sample(range(110), 2))
            known.append(
                {
                    "start": left / 1.1,
                    "end": right / 1.1,
                    "reason": rng.choice(
                        ["BUFFER_DROPPED", "CAMERA_DISCONNECTED", "RECORDING_DISABLED"]
                    ),
                }
            )
        assert build_gaps(spans, known) == reference_gaps(spans, known)


def test_gap_crossing_multiple_saved_intervals_and_unknown_threshold():
    known = [{"start": 0.5, "end": 7, "reason": "BUFFER_DROPPED"}]
    assert build_gaps([(0, 1), (2, 4), (6, 8), (10, 12)], known) == reference_gaps(
        [(0, 1), (2, 4), (6, 8), (10, 12)], known
    )


@pytest.mark.asyncio
async def test_thousands_of_short_losses_do_not_block_recording_queue(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 15))
    clock = [window.start_at]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(clock[0])
    day = (await daily.list_days("camera-main", {}, 20, None))[0]
    with service.db:
        for i in range(3000):
            start = window.start_at + i / 10
            segment = daily.catalog.begin_segment(day["recording_id"], start, start + 0.05)
            daily.catalog.complete_segment(
                segment["segment_id"],
                {
                    "start_at": start,
                    "end_at": start + 0.05,
                    "duration": 0.05,
                    "size_bytes": 0,
                    "path": "scale.ts",
                    "profile": daily.profile,
                },
            )
            daily.notify_loss("camera-main", start + 0.05, start + 0.1, "BUFFER_DROPPED")
        daily._flush_losses(window.start_at + 300)
        started = time.monotonic()
        result = daily._refresh(day, window.start_at + 300)
        elapsed = time.monotonic() - started
    assert len(result["gaps"]) == 3000
    assert len(result["available_intervals"]) == 3000
    assert result["recorded_seconds"] == 150
    # The live queue holds roughly 3 seconds at 30fps. Catalog updates must finish well below that.
    assert elapsed < 1.5, f"catalog update blocked the shared writer for {elapsed:.3f}s"
    # This fixture represents catalogue entries, not encoded files; no tail should be scheduled.
    daily.initialized = False
    await daily.close()
    await service.close()


def test_prune_query_remains_fast_with_many_frames_and_pins(tmp_path):
    from camera_media_service.pipeline import LiveFrameHub
    from camera_media_service.recording import RecordingService
    from camera_media_service.store import LatestFrameStore

    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    try:
        with service.db:
            service.db.executemany(
                "INSERT INTO frames VALUES(?,?,?,?)",
                ((str(i), "camera-main", float(i), 1) for i in range(30000)),
            )
            service.db.executemany(
                "INSERT INTO frame_pins VALUES(?,?)", (("active", str(i)) for i in range(3000))
            )
        started = time.monotonic()
        rows = service.db.execute(
            "SELECT path FROM frames f WHERE timestamp<10000 "
            "AND NOT EXISTS(SELECT 1 FROM frame_pins p WHERE p.path=f.path) "
            "ORDER BY timestamp"
        ).fetchall()
        elapsed = time.monotonic() - started
        assert len(rows) == 7000
        assert elapsed < 0.5, f"pin lookups blocked the writer for {elapsed:.3f}s"
    finally:
        service.db.close()
