"""HLS playback and mapping between recorded media and Korean calendar time."""

from __future__ import annotations

from camera_media_service.recording import epoch, iso


def build_available_intervals(segments: list[dict], gaps: list[dict] = ()) -> list[dict]:
    intervals = []
    offset = 0.0
    losses = sorted((epoch(g["start_at"]), epoch(g["end_at"])) for g in gaps)
    loss_cursor = 0
    for segment in segments:
        end = offset + segment["duration"]
        previous_end = epoch(intervals[-1]["end_at"]) if intervals else None
        if previous_end is not None:
            while loss_cursor < len(losses) and losses[loss_cursor][1] <= previous_end:
                loss_cursor += 1
        known_loss = (
            previous_end is not None
            and loss_cursor < len(losses)
            and losses[loss_cursor][0] < segment["start_at"]
        )
        if intervals and 0 <= segment["start_at"] - previous_end <= 1 and not known_loss:
            intervals[-1].update(end_at=iso(segment["end_at"]), playback_end_seconds=round(end, 3))
        else:
            intervals.append(
                {
                    "start_at": iso(segment["start_at"]),
                    "end_at": iso(segment["end_at"]),
                    "playback_start_seconds": round(offset, 3),
                    "playback_end_seconds": round(end, 3),
                }
            )
        offset = end
    return intervals


def build_daily_playlist(day: dict, segments: list[dict]) -> str:
    closed = day["status"] == "closed"
    target = day.get("hls_target_duration", 10)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        "#EXT-X-PLAYLIST-TYPE:" + ("VOD" if closed else "EVENT"),
    ]
    previous = None
    for segment in segments:
        if previous is not None:
            # Independent encoders reset PTS even when the wall-clock spans are contiguous.
            lines.append("#EXT-X-DISCONTINUITY")
        lines.extend(
            (
                f"#EXT-X-PROGRAM-DATE-TIME:{iso(segment['start_at'])}",
                f"#EXTINF:{segment['duration']:.6f},",
                f"/api/v2/daily-recordings/{day['recording_id']}/media/{segment['segment_id']}.ts",
            )
        )
        previous = segment
    if closed:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
