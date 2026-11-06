from datetime import date

import imageio_ffmpeg
import pytest
from test_daily_recording import jpeg, make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.pipeline import FrameEnvelope
from camera_media_service.recording import StorageCapacityError, iso


@pytest.mark.asyncio
async def test_late_recorder_catches_up_all_existing_frames_in_bounded_ticks(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(clock[0])
        raw = jpeg()
        await recording.store_frames(
            [FrameEnvelope("camera-main", start + i, i, raw) for i in range(60)]
        )
        clock[0] = start + 60
        for _ in range(5):
            await daily.tick(clock[0])
            if recording.db.execute("SELECT cursor FROM daily_cursors").fetchone()[0] >= clock[0]:
                break
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert body["recorded_seconds"] >= 59.9
        assert not any(g["reason_code"] == "BUFFER_DROPPED" for g in body["gaps"])
        assert not recording.db.execute("SELECT 1 FROM frame_pins LIMIT 1").fetchone()
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_pending_daily_frames_survive_age_pruning(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(start)
        await recording.store_frames(
            [FrameEnvelope("camera-main", start + i, i, jpeg()) for i in range(3)]
        )
        clock[0] += 400
        async with recording.lock:
            await recording._prune(clock[0])
        assert recording.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 3
        assert len(list(recording.buffer.glob("*.jpg"))) == 3
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_full_buffer_refuses_new_input_without_evicting_pending_frames(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    raw = jpeg()
    recording.max_buffer_bytes = len(raw) * 3
    try:
        await daily.tick(start)
        await recording.store_frames(
            [FrameEnvelope("camera-main", start + i, i, raw) for i in range(3)]
        )
        original = {p.name for p in recording.buffer.glob("*.jpg")}
        clock[0] += 400
        with pytest.raises(StorageCapacityError):
            await recording.store_frame(FrameEnvelope("camera-main", clock[0], 4, raw))
        assert {p.name for p in recording.buffer.glob("*.jpg")} == original
        assert recording.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 3
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_segment_revision_survives_restart_without_rewriting_day_json(tmp_path, monkeypatch):
    from test_recording import make_service

    from camera_media_service.daily_catalog import DailyCatalog

    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    reopened = None
    try:
        await daily.tick(start)
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        key = body["recording_id"]
        with recording.db:
            segment = daily.catalog.begin_segment(key, start, start + 1)
        before = recording.db.execute(
            "SELECT body FROM daily_recordings WHERE id=?", (key,)
        ).fetchone()[0]
        with recording.db:
            daily.catalog.complete_segment(
                segment["segment_id"],
                {
                    "start_at": start,
                    "end_at": start + 1,
                    "duration": 1,
                    "size_bytes": 0,
                    "path": "synthetic.ts",
                    "profile": daily.profile,
                },
            )
        assert (await daily.get_day(key))["revision"] == body["revision"] + 1
        assert (await daily.list_days("camera-main", {}, 20, None))[0]["revision"] == body[
            "revision"
        ] + 1
        assert (
            recording.db.execute("SELECT body FROM daily_recordings WHERE id=?", (key,)).fetchone()[
                0
            ]
            == before
        )
        daily.initialized = False
        await daily.close()
        await recording.close()
        reopened = make_service(tmp_path)
        assert DailyCatalog(reopened.db).get_day(key)["revision"] == body["revision"] + 1
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()
        if reopened:
            await reopened.close()


@pytest.mark.asyncio
async def test_completed_slot_never_rewrites_whole_day(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(start)
        await recording.store_frames(
            [FrameEnvelope("camera-main", start + i / 10, i, jpeg()) for i in range(10)]
        )
        statements = []
        recording.db.set_trace_callback(statements.append)
        clock[0] += 10
        await daily.tick(clock[0])
        recording.db.set_trace_callback(None)
        writes = [s for s in statements if s.startswith("UPDATE daily_recordings SET")]
        assert not writes
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert body["recorded_seconds"] >= 0.9 and body["revision"] == 1
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_idle_ticks_do_not_repeatedly_read_large_day_json(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(start)
        statements = []
        recording.db.set_trace_callback(statements.append)
        clock[0] += 0.3
        for _ in range(5):
            await daily.tick(clock[0])
        recording.db.set_trace_callback(None)
        reads = [s for s in statements if "SELECT body FROM daily_recordings" in s]
        assert not reads
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_frame_commits_run_off_event_loop(tmp_path, monkeypatch):
    import threading

    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    main_thread = threading.get_ident()
    commits = []
    try:
        recording.db.set_trace_callback(
            lambda sql: commits.append(threading.get_ident()) if sql == "COMMIT" else None
        )
        await recording.store_frame(FrameEnvelope("camera-main", start, 1, jpeg()))
        assert commits and all(thread != main_thread for thread in commits)
    finally:
        recording.db.set_trace_callback(None)
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_restart_preserves_and_records_durable_backlog_before_interruption(
    tmp_path, monkeypatch
):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(start)
    raw = jpeg()
    await recording.store_frames(
        [FrameEnvelope("camera-main", start + i, i, raw) for i in range(60)]
    )
    clock[0] = start + 60
    await recording.close()  # Crash boundary: do not run the daily shutdown tail.
    clock[0] = start + 400
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        async with recording.lock:
            await recording._prune(clock[0])
        assert recording.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 60
        for _ in range(6):
            await daily.tick(clock[0])
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert body["recorded_seconds"] >= 59.9
        assert all(g["start_at"] >= iso(start + 60) for g in body["gaps"])
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["daily_budget", "total_budget", "minimum_free"])
async def test_storage_rejection_reports_exact_internal_condition(tmp_path, monkeypatch, condition):
    from camera_media_service.recording import ApiError

    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    recording, daily = make_daily(tmp_path, [start], monkeypatch)
    try:
        reserve = 0
        if condition == "daily_budget":
            daily.settings.max_daily_storage_bytes = 1
            reserve = 2
        elif condition == "total_budget":
            daily.settings.max_media_storage_bytes = 1
        else:
            daily.settings.min_free_storage_bytes = 10**30
        with pytest.raises(ApiError) as error:
            await daily.check_space(reserve)
        assert error.value.code == "INSUFFICIENT_STORAGE" and error.value.status == 503
        assert error.value.condition == condition
        assert recording.diagnostics.snapshot()["loss_counts"]["storage_rejected_" + condition] == 1
    finally:
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_eighty_thousand_historical_gaps_survive_new_recording_with_one_snapshot(
    tmp_path, monkeypatch
):
    import hashlib
    import json

    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(start)
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        with recording.db:
            recording.db.executemany(
                "INSERT INTO daily_gaps(camera,start,end,reason) VALUES(?,?,?,?)",
                [
                    ("camera-main", start + i / 10, start + i / 10 + 0.05, "BUFFER_DROPPED")
                    for i in range(40000)
                ],
            )
            body = daily._refresh(body, start + 4000)
            recording.db.execute("UPDATE daily_cursors SET cursor=?", (start + 4000,))
        recording.daily_frame_watermarks["camera-main"] = start + 4000
        expected = hashlib.sha256(json.dumps(body["gaps"]).encode()).hexdigest()
        assert len(body["gaps"]) == 80000
        assert len(json.dumps(body)) > 8_000_000
        raw = jpeg()
        await recording.store_frames(
            [FrameEnvelope("camera-main", start + 4000 + i / 10, i, raw) for i in range(10)]
        )
        statements = []
        recording.db.set_trace_callback(lambda sql: statements.append(sql[:60]))
        clock[0] = start + 4010
        await daily.tick(clock[0])
        recording.db.set_trace_callback(None)
        actual = await daily.get_day(body["recording_id"])
        prefix = [g for g in actual["gaps"] if g["end_at"] <= iso(start + 4000)]
        assert hashlib.sha256(json.dumps(prefix).encode()).hexdigest() == expected
        assert actual["recorded_seconds"] >= 0.9
        assert not any(s.startswith("UPDATE daily_recordings SET") for s in statements)
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_old_empty_prefix_never_skips_recent_buffered_frames(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await daily.tick(start)
        raw = jpeg()
        await recording.store_frames(
            [FrameEnvelope("camera-main", start + 80 + i, i, raw) for i in range(10)]
        )
        clock[0] = start + 100
        for _ in range(5):
            await daily.tick(clock[0])
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert body["recorded_seconds"] >= 9.9
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_cancellation_during_durable_frame_commit_keeps_committed_jpegs(
    tmp_path, monkeypatch
):
    import asyncio
    import threading

    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    recording, daily = make_daily(tmp_path, [start], monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def gated(sql):
        if sql == "COMMIT":
            entered.set()
            release.wait(3)

    recording.db.set_trace_callback(gated)
    task = asyncio.create_task(
        recording.store_frame(FrameEnvelope("camera-main", start, 1, jpeg()))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        rows = recording.db.execute("SELECT path FROM frames").fetchall()
        assert len(rows) == 1
        assert all((recording.buffer / row[0]).is_file() for row in rows)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        recording.db.set_trace_callback(None)
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_restart_during_encoder_reuses_durable_processing_segment_frames(
    tmp_path, monkeypatch
):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    clock = [start]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    await daily.tick(start)
    raw = jpeg()
    await recording.store_frames(
        [FrameEnvelope("camera-main", start + i, i, raw) for i in range(20)]
    )
    body = (await daily.list_days("camera-main", {}, 20, None))[0]
    with recording.db:
        segment = daily.catalog.begin_segment(body["recording_id"], start, start + 10)
        recording.pin_frames(segment["segment_id"], "camera-main", start, start + 10)
    clock[0] = start + 20
    await recording.close()
    clock[0] = start + 30
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        for _ in range(3):
            await daily.tick(clock[0])
        body = (await daily.list_days("camera-main", {}, 20, None))[0]
        assert body["recorded_seconds"] >= 19.9
        assert all(g["start_at"] >= iso(start + 20) for g in body["gaps"])
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()
