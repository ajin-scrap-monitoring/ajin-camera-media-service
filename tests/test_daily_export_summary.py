import asyncio
import json
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_export import make_exports, recorded_day

from camera_media_service.daily_queries import DailySnapshots
from camera_media_service.http_api import create_http_app


async def owner(tmp_path, monkeypatch):
    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def idle():
        pass

    monkeypatch.setattr(exports, "start", idle)
    app = create_http_app(
        recording, daily=daily, exports=exports, thumbnail_options={"enabled": False}
    )
    return recording, daily, exports, clock, day, app


@pytest.mark.asyncio
async def test_large_export_summary_never_reads_timelines_and_starts_no_jobs(tmp_path, monkeypatch):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    key = day["recording_id"]
    day["gaps"] = [
        {"start_at": str(i), "end_at": str(i + 1), "reason_code": "STORAGE_FULL"}
        for i in range(65000)
    ]
    day["available_intervals"] = [
        {
            "start_at": "same",
            "end_at": "same",
            "playback_start_seconds": 0,
            "playback_end_seconds": 0.05,
        }
    ]
    with recording.db:
        daily.catalog.save_day(day)
        daily.catalog.advance_revision(key)
    async with TestClient(TestServer(app)) as client:
        path = "/api/v2/daily-recordings/" + key
        legacy = await client.post(path + "/exports")
        assert (
            legacy.status == 503
            and (await legacy.json())["error"]["code"] == "DAILY_RESPONSE_TOO_LARGE"
        )
        expected = await (await client.get(path + "/summary")).json()

        def fail(*args, **kwargs):
            raise AssertionError(
                "light export must not read full snapshots or sparse timeline indices"
            )

        monkeypatch.setattr(DailySnapshots, "read", fail)
        monkeypatch.setattr(DailySnapshots, "_index", fail)
        before = list(
            recording.db.execute(
                "SELECT sequence,body FROM daily_snapshot_deltas WHERE day_id=?", (key,)
            )
        )
        response = await client.post(path + "/exports/summary", json={})
        assert response.status == 202
        assert response.headers["Location"] == path + "/summary"
        assert response.headers["Cache-Control"] == "no-store"
        raw = await response.read()
        assert len(raw) < 4096
        result = json.loads(raw)
        assert result == expected
        assert not {"gaps", "available_intervals", "segments"} & result.keys()
        assert (
            list(
                recording.db.execute(
                    "SELECT sequence,body FROM daily_snapshot_deltas WHERE day_id=?", (key,)
                )
            )
            == before
        )
        assert not exports.jobs and not list(exports.output.glob("*.mp4"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expected", [("not_requested", 202), ("processing", 202), ("failed", 202), ("ready", 200)]
)
async def test_export_summary_states_and_existing_response_compatible(
    tmp_path, monkeypatch, state, expected
):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    key = day["recording_id"]
    cached = daily.catalog.get_state(key)
    cached.update(
        export_state=state,
        error={"code": "ENCODING_FAILED", "message": "Synthetic failure"}
        if state == "failed"
        else None,
    )
    archive = exports.output / (key + ".mp4")
    if state == "ready":
        archive.write_bytes(b"verified-fixture-header")
        cached.update(archive_state="mp4", size_bytes=archive.stat().st_size)
    with recording.db:
        daily.catalog.save_state(cached)
    async with TestClient(TestServer(app)) as client:
        path = "/api/v2/daily-recordings/" + key
        before = archive.stat().st_mtime_ns if archive.exists() else None
        response = await client.post(path + "/exports/summary")
        assert response.status == expected
        result = await response.json()
        assert result["export_state"] == state and result["error"] == cached["error"]
        assert result == await (await client.get(path + "/summary")).json()
        full = await client.post(path + "/exports")
        assert full.status == expected and full.headers["Location"] == path
        assert "gaps" in await full.json()
        assert not exports.jobs
        if before is not None:
            assert archive.stat().st_mtime_ns == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,status,code",
    [
        ({"status": "deleted"}, 410, "DAILY_RECORDING_DELETED"),
        ({"status": "expired"}, 410, "DAILY_RECORDING_EXPIRED"),
        ({"status": "finalizing"}, 409, "DAY_FINALIZING"),
        ({"recorded_seconds": 0}, 409, "NO_RECORDING_DATA"),
        ({"archive_state": "mp4", "export_state": "ready"}, 503, "RECORDING_STORAGE_ERROR"),
    ],
)
async def test_export_summary_preserves_export_validation_errors(
    tmp_path, monkeypatch, change, status, code
):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    cached = daily.catalog.get_state(day["recording_id"])
    cached.update(change)
    with recording.db:
        daily.catalog.save_state(cached)
    async with TestClient(TestServer(app)) as client:
        path = "/api/v2/daily-recordings/" + day["recording_id"]
        for suffix in ("/exports", "/exports/summary"):
            response = await client.post(path + suffix)
            assert response.status == status and (await response.json())["error"]["code"] == code


@pytest.mark.asyncio
async def test_light_export_invalid_requests_and_missing_cache_do_not_replay_history(
    tmp_path, monkeypatch
):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        path = "/api/v2/daily-recordings/" + day["recording_id"] + "/exports/summary"
        assert (await client.post(path + "?view=summary")).status == 400
        assert (await client.post(path, json={"start_at": "unexpected"})).status == 400
        assert (await client.post(path, json=[])).status == 400
        assert (await client.post("/api/v2/daily-recordings/missing/exports/summary")).status == 404
        today = recording.db.execute(
            "SELECT id FROM daily_recordings WHERE status='recording'"
        ).fetchone()[0]
        response = await client.post("/api/v2/daily-recordings/" + today + "/exports/summary")
        assert (
            response.status == 409 and (await response.json())["error"]["code"] == "DAY_NOT_CLOSED"
        )
        key = day["recording_id"]
        with recording.db:
            recording.db.execute("DELETE FROM daily_states WHERE day_id=?", (key,))

        def fail(*args, **kwargs):
            raise AssertionError("missing scalar cache must never replay history")

        monkeypatch.setattr(daily.catalog, "get_day", fail)
        response = await client.post(path)
        assert (
            response.status == 503
            and (await response.json())["error"]["code"] == "CATALOG_SUMMARY_UNAVAILABLE"
        )
        with recording.db:
            recording.db.execute(
                "INSERT INTO daily_states VALUES(?,?)",
                (key, json.dumps({"recording_id": key, "private": "x" * 70000})),
            )
        response = await client.post(path)
        assert (
            response.status == 503
            and (await response.json())["error"]["code"] == "CATALOG_SUMMARY_UNAVAILABLE"
        )


@pytest.mark.asyncio
async def test_checked_summary_runs_off_loop_and_cancel_keeps_catalogue_lock(tmp_path, monkeypatch):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = daily.catalog.get_summary_state
    main_thread = threading.get_ident()

    def slow(key):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(5)
        return original(key)

    monkeypatch.setattr(daily.catalog, "get_summary_state", slow)
    task = asyncio.create_task(exports.request_export(day["recording_id"], summary=True))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert daily.lock.locked()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and daily.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not daily.lock.locked() and not exports.jobs
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        daily.initialized = False
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("recorded_seconds", "bad"),
        ("recorded_seconds", float("nan")),
        ("recorded_seconds", float("inf")),
        ("window_end_at", "bad"),
        ("expires_at", "bad"),
        ("export_state", []),
    ],
)
async def test_invalid_scalar_cache_fails_closed_without_history_fallback(
    tmp_path, monkeypatch, field, value
):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        state = daily.catalog.get_state(day["recording_id"])
        state[field] = value
        with recording.db:
            recording.db.execute(
                "UPDATE daily_states SET body=? WHERE day_id=?",
                (json.dumps(state), day["recording_id"]),
            )
        response = await client.post(
            "/api/v2/daily-recordings/" + day["recording_id"] + "/exports/summary"
        )
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "CATALOG_SUMMARY_UNAVAILABLE"
        with recording.db:
            recording.db.execute(
                "UPDATE daily_states SET body=? WHERE day_id=?",
                (json.dumps(daily.catalog.get_day(day["recording_id"])), day["recording_id"]),
            )


@pytest.mark.asyncio
async def test_summary_rechecks_storage_guard_after_initial_http_boundary(tmp_path, monkeypatch):
    recording, daily, exports, clock, day, app = await owner(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        original = recording.storage_guard
        for suffix in ("/exports", "/exports/summary"):
            calls = 0

            def changing():
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise OSError("Synthetic witness changed after request entry")
                original()

            monkeypatch.setattr(recording, "storage_guard", changing)
            try:
                response = await client.post(
                    "/api/v2/daily-recordings/" + day["recording_id"] + suffix
                )
                assert response.status == 503
                assert (await response.json())["error"]["code"] == "STORAGE_UNAVAILABLE"
            finally:
                monkeypatch.setattr(recording, "storage_guard", original)
