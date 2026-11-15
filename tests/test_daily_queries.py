import json
import sqlite3

import pytest

from camera_media_service.daily_queries import DailySnapshots
from camera_media_service.recording import ApiError


def fixture(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE daily_recordings(id TEXT PRIMARY KEY,camera,date,status,body TEXT);
        CREATE TABLE daily_snapshot_deltas(sequence INTEGER PRIMARY KEY,day_id,body TEXT);
        CREATE TABLE daily_revisions(day_id PRIMARY KEY,revision);
    """)
    body = {
        "recording_id": "day",
        "camera_id": "camera-main",
        "recording_date": "2026-11-14",
        "status": "closed",
        "revision": 2,
        "gaps": [{"n": i} for i in range(2100)],
        "available_intervals": [{"start_at": "same", "end_at": "same", "duration": 0.05}],
        "segments": [{"private": "hidden"}],
        "internal_path": "/private",
        "segment_seconds": 20,
        "hls_target_duration": 21,
        "storage_profile": {"width": 1920, "height": 1080, "fps": 15},
    }
    db.execute(
        "INSERT INTO daily_recordings VALUES(?,?,?,?,?)",
        ("day", "camera-main", "2026-11-14", "closed", json.dumps(body)),
    )
    patch = {
        "values": {"recorded_seconds": 12},
        "arrays": {"gaps": {"keep": 1025, "append": [{"n": "한글"}, {"n": 9999}]}},
    }
    db.execute(
        "INSERT INTO daily_snapshot_deltas(day_id,body) VALUES(?,?)", ("day", json.dumps(patch))
    )
    db.execute("INSERT INTO daily_revisions VALUES('day',3)")
    db.commit()
    return db, path, body


def test_projected_snapshot_pages_preserve_suffix_replacement_and_utf8(tmp_path):
    db, path, body = fixture(tmp_path)
    reader = DailySnapshots(path)
    before = list(db.iterdump())
    summary = reader.read("day", summary=True)
    assert summary["revision"] == 3 and summary["recorded_seconds"] == 12
    for field in ("segment_seconds", "hls_target_duration", "storage_profile"):
        assert summary[field] == body[field]
    assert not {"segments", "internal_path", "gaps", "available_intervals"} & summary.keys()
    actual = []
    offset = 0
    while True:
        page = reader.page("day", "gaps", offset, 97)
        actual.extend(page["items"])
        assert page["total"] == 1027 and page["revision"] == 3
        offset = page["next_offset"]
        if offset is None:
            break
    assert actual == body["gaps"][:1025] + [{"n": "한글"}, {"n": 9999}]
    assert reader.read("day")["gaps"] == actual
    assert reader.page("day", "available_intervals", 0, 10)["items"] == body["available_intervals"]
    assert list(db.iterdump()) == before
    assert reader.index_builds == 1
    db.close()


def test_large_legacy_is_explicit_error_summary_and_pages_still_work(tmp_path):
    db, path, _ = fixture(tmp_path)
    reader = DailySnapshots(path, response_bytes=2048)
    with pytest.raises(ApiError) as error:
        reader.read("day")
    assert error.value.status == 503 and error.value.code == "DAILY_RESPONSE_TOO_LARGE"
    assert reader.read("day", summary=True)["recording_id"] == "day"
    assert len(reader.page("day", "gaps", 0, 10)["items"]) == 10
    db.close()


def test_revision_and_snapshot_change_invalidate_pages_without_losing_history(tmp_path):
    db, path, _ = fixture(tmp_path)
    reader = DailySnapshots(path)
    first = reader.page("day", "gaps", 0, 10)
    db.execute(
        "INSERT INTO daily_snapshot_deltas(day_id,body) VALUES(?,?)",
        (
            "day",
            json.dumps(
                {
                    "values": {"status": "deleted"},
                    "arrays": {"gaps": {"keep": 1, "append": [{"last": True}]}},
                }
            ),
        ),
    )
    db.commit()
    with pytest.raises(ApiError) as error:
        reader.page("day", "gaps", 10, 10, fingerprint=first["snapshot"])
    assert error.value.status == 409 and error.value.code == "DAILY_SNAPSHOT_CHANGED"
    fresh = reader.page("day", "gaps", 0, 10)
    assert fresh["items"] == [{"n": 0}, {"last": True}]
    assert reader.read("day", summary=True)["status"] == "deleted"
    db.close()
