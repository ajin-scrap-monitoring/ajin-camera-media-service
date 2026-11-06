import hashlib
import json
from datetime import date

import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_catalog import DailyCatalog
from camera_media_service.daily_models import DailyWindow


@pytest.mark.asyncio
async def test_large_snapshot_changes_are_small_durable_writes(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 6)).start_at]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        day["gaps"] = [
            {"start_at": str(i), "end_at": str(i + 1), "reason_code": "BUFFER_DROPPED"}
            for i in range(10000)
        ]
        # Model a legacy production catalogue whose large snapshot already exists.
        with recording.db:
            recording.db.execute(
                "UPDATE daily_recordings SET body=? WHERE id=?",
                (json.dumps(day), day["recording_id"]),
            )
        before = recording.db.execute(
            "SELECT body FROM daily_recordings WHERE id=?", (day["recording_id"],)
        ).fetchone()[0]
        changed = daily.catalog.get_day(day["recording_id"])
        changed["gaps"][-1]["end_at"] = "10001"
        changed["recorded_seconds"] = 12.5
        statements = []
        recording.db.set_trace_callback(statements.append)
        with recording.db:
            daily.catalog.save_day(changed)
        recording.db.set_trace_callback(None)
        after = recording.db.execute(
            "SELECT body FROM daily_recordings WHERE id=?", (day["recording_id"],)
        ).fetchone()[0]
        assert (
            hashlib.sha256(after.encode()).hexdigest()
            == hashlib.sha256(before.encode()).hexdigest()
        )
        assert max(len(s) for s in statements) < 10000
        assert DailyCatalog(recording.db).get_day(day["recording_id"]) == changed
        with recording.db:
            changed["status"] = "deleted"
            daily.catalog.save_day(changed)
        restored = DailyCatalog(recording.db).get_day(day["recording_id"])
        assert restored == changed
        assert (
            recording.db.execute(
                "SELECT status FROM daily_recordings WHERE id=?", (day["recording_id"],)
            ).fetchone()[0]
            == "deleted"
        )
        with recording.db:
            daily.catalog.materialize_snapshots()
        assert (
            json.loads(
                recording.db.execute(
                    "SELECT body FROM daily_recordings WHERE id=?", (day["recording_id"],)
                ).fetchone()[0]
            )
            == changed
        )
        assert not recording.db.execute("SELECT 1 FROM daily_snapshot_deltas").fetchone()
    finally:
        recording.db.set_trace_callback(None)
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_snapshot_suffix_replacement_and_rollback_are_atomic(tmp_path, monkeypatch):
    from copy import deepcopy

    clock = [DailyWindow.from_date(date(2026, 11, 6)).start_at]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        day["gaps"] = [{"first": 1}, {"tail": 2}]
        day["available_intervals"] = [{"span": 1}]
        with recording.db:
            daily.catalog.save_day(day)
        changed = deepcopy(day)
        changed["gaps"] = [{"first": 1}, {"tail": 3}, {"later": 4}]
        changed["available_intervals"] = []
        changed["archive_state"] = "mp4"
        with pytest.raises(RuntimeError), recording.db:
            daily.catalog.save_day(changed)
            raise RuntimeError("interrupted transaction")
        assert daily.catalog.get_day(day["recording_id"]) == day
        assert daily.catalog.get_state(day["recording_id"])["archive_state"] != "mp4"
        with recording.db:
            daily.catalog.save_day(changed)
        assert daily.catalog.get_day(day["recording_id"]) == changed
        assert daily.catalog.get_state(day["recording_id"])["archive_state"] == "mp4"
        with recording.db:
            daily.catalog.materialize_snapshots()
            # A legacy binary changes the base after the documented rollback preparation.
            changed["archive_state"] = "segments"
            recording.db.execute(
                "UPDATE daily_recordings SET body=? WHERE id=?",
                (json.dumps(changed), changed["recording_id"]),
            )
        reopened = DailyCatalog(recording.db)
        assert reopened.get_state(day["recording_id"])["archive_state"] == "segments"
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_status_changes_do_not_rewrite_legacy_overflow_pages(tmp_path, monkeypatch):
    clock = [DailyWindow.from_date(date(2026, 11, 6)).start_at]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        day["gaps"] = [{"loss": i, "reason_code": "BUFFER_DROPPED"} for i in range(100000)]
        with recording.db:
            recording.db.execute(
                "UPDATE daily_recordings SET body=? WHERE id=?",
                (json.dumps(day), day["recording_id"]),
            )
        catalog = DailyCatalog(recording.db)
        recording.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        recording.db.execute("PRAGMA wal_autocheckpoint=0")
        day["status"] = "closed"
        with recording.db:
            catalog.save_day(day)
        assert (recording.root / "catalog.sqlite3-wal").stat().st_size < 100000
        assert catalog.get_day(day["recording_id"]) == day
        assert catalog.get_state(day["recording_id"])["status"] == "closed"
        with recording.db:
            catalog.materialize_snapshots()
        assert (
            json.loads(
                recording.db.execute(
                    "SELECT body FROM daily_recordings WHERE id=?", (day["recording_id"],)
                ).fetchone()[0]
            )
            == day
        )
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_restart_compacts_replay_and_rollback_keeps_revision_only_progress(
    tmp_path, monkeypatch
):
    clock = [DailyWindow.from_date(date(2026, 11, 6)).start_at]
    recording, daily = make_daily(tmp_path, clock, monkeypatch)
    try:
        await daily.tick(clock[0])
        day = (await daily.list_days("camera-main", {}, 20, None))[0]
        key = day["recording_id"]
        with recording.db:
            recording.db.executemany(
                "INSERT INTO daily_snapshot_deltas(day_id,body) VALUES(?,?)",
                (
                    (key, json.dumps({"values": {"recorded_seconds": i}, "arrays": {}}))
                    for i in range(10000)
                ),
            )
        catalog = DailyCatalog(recording.db)
        assert catalog.get_day(key)["recorded_seconds"] == 9999
        assert catalog.get_state(key)["recorded_seconds"] == 9999
        assert recording.db.execute("SELECT count(*) FROM daily_snapshot_deltas").fetchone()[0] <= 1
        with recording.db:
            catalog.materialize_snapshots()
            catalog.advance_revision(key)
        assert not recording.db.execute("SELECT 1 FROM daily_snapshot_deltas").fetchone()
        with recording.db:
            catalog.materialize_snapshots()
        raw = json.loads(
            recording.db.execute("SELECT body FROM daily_recordings WHERE id=?", (key,)).fetchone()[
                0
            ]
        )
        assert raw["revision"] == 1
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()
