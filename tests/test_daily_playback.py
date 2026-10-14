import importlib
import importlib.util

from camera_media_service.recording import epoch


def playback():
    assert importlib.util.find_spec("camera_media_service.daily_playback"), "Daily playback missing"
    return importlib.import_module("camera_media_service.daily_playback")


def test_normal_frame_jitter_merges_but_confirmed_short_loss_does_not():
    from camera_media_service.recording import iso

    segments = [
        {"start_at": 100.0, "end_at": 110.0, "duration": 10.0},
        {"start_at": 110.02, "end_at": 120.0, "duration": 9.98},
    ]
    assert len(playback().build_available_intervals(segments)) == 1
    assert (
        len(
            playback().build_available_intervals(
                segments, [{"start_at": iso(110), "end_at": iso(110.02)}]
            )
        )
        == 2
    )


def fixture():
    start = epoch("2026-10-13T15:00:00.000Z")
    day = {"recording_id": "day-id", "status": "recording"}
    segments = [
        {"segment_id": "one", "start_at": start, "end_at": start + 10, "duration": 10.0},
        {"segment_id": "two", "start_at": start + 20, "end_at": start + 30, "duration": 10.0},
    ]
    return day, segments


def test_today_hls_contains_only_saved_media_and_preserves_gap_clock():
    day, segments = fixture()
    text = playback().build_daily_playlist(day, segments)
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in text
    assert "#EXT-X-ENDLIST" not in text
    assert "#EXT-X-PROGRAM-DATE-TIME:2026-10-13T15:00:20.000Z" in text
    assert "#EXT-X-DISCONTINUITY" in text
    assert "/api/v2/daily-recordings/day-id/media/two.ts" in text
    intervals = playback().build_available_intervals(segments)
    assert intervals[1]["playback_start_seconds"] == 10
    assert intervals[1]["start_at"] == "2026-10-13T15:00:20.000Z"


def test_closed_hls_is_vod_and_contiguous_intervals_merge():
    day, segments = fixture()
    day["status"] = "closed"
    segments[1]["start_at"] -= 10
    segments[1]["end_at"] -= 10
    text = playback().build_daily_playlist(day, segments)
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and "#EXT-X-ENDLIST" in text
    intervals = playback().build_available_intervals(segments)
    assert len(intervals) == 1 and intervals[0]["playback_end_seconds"] == 20


def test_each_independent_ts_timestamp_reset_is_declared():
    day, segments = fixture()
    segments[1]["start_at"] -= 10
    segments[1]["end_at"] -= 10
    assert playback().build_daily_playlist(day, segments).count("#EXT-X-DISCONTINUITY\n") == 1


def test_live_target_duration_remains_fixed_after_short_first_segment():
    day, segments = fixture()
    segments[0]["duration"] = 1
    short = playback().build_daily_playlist(day, segments[:1])
    full = playback().build_daily_playlist(day, segments)
    def target(text):
        return next(line for line in text.splitlines() if line.startswith("#EXT-X-TARGETDURATION:"))
    assert target(short) == target(full)
