import io

import pytest


def test_complete_jpeg_reader_counts_frames_not_transport_chunks():
    from tools.media_delivery_probe import read_mjpeg_frame

    jpeg = b"\xff\xd8payload\xff\xd9"
    wire = (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(jpeg)).encode()
        + b"\r\n\r\n"
        + jpeg
        + b"\r\n"
    )
    stream = io.BytesIO(wire + wire)
    assert read_mjpeg_frame(stream) == len(jpeg)
    assert read_mjpeg_frame(stream) == len(jpeg)
    assert read_mjpeg_frame(stream) is None


def test_mjpeg_reader_rejects_truncated_or_nonjpeg_frames():
    from tools.media_delivery_probe import read_mjpeg_frame

    with pytest.raises(ValueError):
        read_mjpeg_frame(io.BytesIO(b"--f\r\nContent-Length: 10\r\n\r\nshort"))
    with pytest.raises(ValueError):
        read_mjpeg_frame(io.BytesIO(b"--f\r\nContent-Length: 4\r\n\r\nxxxx"))


def test_gap_summary_preserves_wall_time_and_counts_only_complete_intervals():
    from tools.media_delivery_probe import summarize

    result = summarize([100.0, 100.1, 101.3, 101.4])
    assert result["count"] == 4
    assert result["over1s"] == 1
    assert result["first_at"] == 100
    assert result["last_at"] == 101.4
    assert result["max_interval_s"] == pytest.approx(1.2)


def test_durable_snapshot_distinguishes_current_window_from_historical_holes(tmp_path):
    import json
    import sqlite3

    from tools.media_delivery_probe import durable_snapshot

    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE frames(camera,timestamp);"
            "CREATE TABLE daily_recordings(id,body);"
            "CREATE TABLE daily_segments(id INTEGER PRIMARY KEY,day_id,status,start,end);"
        )
        db.execute(
            "INSERT INTO daily_recordings VALUES(?,?)",
            ("day", json.dumps({"recording_date": "2026-11-03", "gaps": []})),
        )
        db.executemany(
            "INSERT INTO daily_segments(day_id,status,start,end) VALUES(?,?,?,?)",
            [
                ("day", "ready", 0.0, 9.0),
                ("day", "ready", 10.0, 20.0),
                ("day", "ready", 100.0, 110.0),
                ("day", "ready", 110.0, 120.0),
            ],
        )
    result = durable_snapshot(path, "camera-main", 99, 121, ["day"])["days"]["day"]
    assert result["positive_segment_holes"] == 2
    assert result["window_ready_segments"] == 2
    assert result["window_boundary_pairs"] == 1
    assert result["window_positive_holes"] == 0
