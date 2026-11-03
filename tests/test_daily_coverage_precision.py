from camera_media_service.daily_gaps import build_gaps
from camera_media_service.daily_playback import build_available_intervals
from camera_media_service.recording import iso


def test_subsecond_uncovered_spans_are_reported_without_reason_suppression():
    assert build_gaps([(100, 100.2)], []) == [
        {"start_at": iso(100), "end_at": iso(100.2), "reason_code": "UNKNOWN_CAPTURE_LOSS"}
    ]


def test_short_unknown_prefix_and_suffix_survive_known_loss_intersection():
    gaps = build_gaps([(100, 101)], [{"start": 100.2, "end": 100.8, "reason": "BUFFER_DROPPED"}])
    assert [g["reason_code"] for g in gaps] == [
        "UNKNOWN_CAPTURE_LOSS",
        "BUFFER_DROPPED",
        "UNKNOWN_CAPTURE_LOSS",
    ]
    assert gaps[0]["end_at"] == gaps[1]["start_at"]
    assert gaps[1]["end_at"] == gaps[2]["start_at"]


def test_playback_never_bridges_uncovered_subsecond_segment_boundary():
    segments = [
        {"start_at": 100.0, "end_at": 110.0, "duration": 10.0},
        {"start_at": 110.02, "end_at": 120.0, "duration": 9.98},
    ]
    intervals = build_available_intervals(segments)
    assert len(intervals) == 2
    assert intervals[0]["end_at"] == iso(110)
    assert intervals[1]["start_at"] == iso(110.02)
