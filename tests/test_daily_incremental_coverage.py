import copy
import hashlib
import json
from datetime import date

import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow


def segment(daily, key, start, duration):
    item = daily.catalog.begin_segment(key, start, start + duration)
    daily.catalog.complete_segment(
        item["segment_id"],
        {
            "start_at": start,
            "end_at": start + duration,
            "duration": duration,
            "size_bytes": 1,
            "path": "metadata-fixture.ts",
            "profile": daily.profile,
        },
    )


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@pytest.mark.asyncio
async def test_incremental_refresh_matches_full_without_rescanning_history(tmp_path, monkeypatch):
    import camera_media_service.daily_recording as module

    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    recording, daily = make_daily(tmp_path, [start], monkeypatch)
    try:
        await daily.tick(start)
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        with recording.db:
            for i in range(1000):
                segment(daily, day["recording_id"], start + i / 2, 0.033333)
            daily._save_loss("camera-main", start + 450, start + 501, "BUFFER_DROPPED")
            daily._refresh(day, start + 500)
        original = module.build_gaps
        visited = []

        def counted(spans, known):
            visited.append((len(spans), len(known)))
            return original(spans, known)

        monkeypatch.setattr(module, "build_gaps", counted)
        for i in range(30):
            with recording.db:
                segment(daily, day["recording_id"], start + 500 + i / 2, 0.033333)
                recording.db.execute("UPDATE daily_gaps SET end=?", (start + 511 + i / 2,))
            until = start + 500 + (i + 1) / 2
            current = daily.catalog.get_day(day["recording_id"])
            rows, known = daily._coverage_rows(current, until)
            expected = daily._refresh_values(
                copy.deepcopy(current), until, [json.loads(r[0]) for r in rows], known
            )
            visited.clear()
            async with recording.lock:
                async with recording.transaction():
                    actual = await daily._refresh_async(current, until)
            assert digest(actual) == digest(expected)
            assert max(count for count, _ in visited) <= 2
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_older_reason_and_duration_changes_invalidate_incremental_prefix(
    tmp_path, monkeypatch
):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    recording, daily = make_daily(tmp_path, [start], monkeypatch)
    try:
        await daily.tick(start)
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        key = day["recording_id"]
        with recording.db:
            segment(daily, key, start, 0.333333)
            daily._save_loss("camera-main", start + 1, start + 2, "BUFFER_DROPPED")
            daily._refresh(day, start + 10)
        for change in ("gap", "duration", "delete", "delete_segment"):
            with recording.db:
                if change == "gap":
                    recording.db.execute("UPDATE daily_gaps SET reason='CAMERA_DISCONNECTED'")
                elif change == "duration":
                    item = daily.catalog.segments(key, ready_only=True)[0]
                    item["duration"] = 0.334667
                    recording.db.execute(
                        "UPDATE daily_segments SET body=? WHERE id=?",
                        (json.dumps(item), item["segment_id"]),
                    )
                elif change == "delete":
                    recording.db.execute("DELETE FROM daily_gaps")
                else:
                    recording.db.execute("DELETE FROM daily_segments")
            current = daily.catalog.get_day(key)
            rows, known = daily._coverage_rows(current, start + 20)
            expected = daily._refresh_values(
                copy.deepcopy(current), start + 20, [json.loads(r[0]) for r in rows], known
            )
            async with recording.lock:
                async with recording.transaction():
                    actual = await daily._refresh_async(current, start + 20)
            assert digest(actual) == digest(expected)
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_refreshing_one_day_does_not_consume_another_days_late_gap_change(
    tmp_path, monkeypatch
):
    current = DailyWindow.from_date(date(2026, 11, 6))
    previous = DailyWindow.from_date(date(2026, 11, 5))
    recording, daily = make_daily(tmp_path, [current.start_at], monkeypatch)
    try:
        await daily.tick(current.start_at)
        with recording.db:
            left = daily.catalog.ensure_day("camera-main", previous, 30)
            right = daily.catalog.ensure_day("camera-main", current, 30)
            segment(daily, left["recording_id"], previous.end_at - 2, 0.333333)
            segment(daily, right["recording_id"], current.start_at + 2, 0.333333)
            daily._save_loss(
                "camera-main", current.start_at - 1, current.start_at + 1, "BUFFER_DROPPED"
            )
            daily._refresh(left, previous.end_at)
            daily._refresh(right, current.start_at + 10)
            recording.db.execute("UPDATE daily_gaps SET reason='CAMERA_DISCONNECTED'")
        for day, until in ((left, previous.end_at), (right, current.start_at + 20)):
            latest = daily.catalog.get_day(day["recording_id"])
            rows, known = daily._coverage_rows(latest, until)
            expected = daily._refresh_values(
                copy.deepcopy(latest), until, [json.loads(r[0]) for r in rows], known
            )
            async with recording.lock:
                async with recording.transaction():
                    actual = await daily._refresh_async(latest, until)
            assert digest(actual) == digest(expected)
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_fractional_boundaries_and_raw_offsets_match_full_refresh(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    recording, daily = make_daily(tmp_path, [start], monkeypatch)
    try:
        await daily.tick(start)
        key = (await daily.list_days("camera-main", {}, 20, None))[0]["recording_id"]
        point = start
        for i in range(60):
            point += (0, 0.0004, 0.0006)[i % 3]
            duration = (0.033333, 0.066667, 0.123456)[i % 3]
            with recording.db:
                segment(daily, key, point, duration)
            point += duration
            current = daily.catalog.get_day(key)
            rows, known = daily._coverage_rows(current, point)
            expected = daily._refresh_values(
                copy.deepcopy(current), point, [json.loads(r[0]) for r in rows], known
            )
            async with recording.lock:
                async with recording.transaction():
                    actual = await daily._refresh_async(current, point)
            assert digest(actual) == digest(expected)
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()
