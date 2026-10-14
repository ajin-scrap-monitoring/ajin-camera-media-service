"""HLS playback and mapping between recorded media and Korean calendar time."""

from __future__ import annotations

import math

from camera_media_service.recording import epoch, iso


def build_available_intervals(segments: list[dict]) -> list[dict]:
    intervals = []
    offset = 0.0
    for segment in segments:
        end = offset + segment["duration"]
        if intervals and abs(epoch(intervals[-1]["end_at"]) - segment["start_at"]) <= 0.001:
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
    target = max(1, math.ceil(max((s["duration"] for s in segments), default=10)))
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
            known_loss = any(
                epoch(g["start_at"]) < segment["start_at"]
                and epoch(g["end_at"]) > previous["end_at"]
                for g in day.get("gaps", [])
            )
            if (
                segment["start_at"] - previous["end_at"] > 1
                or known_loss
                or segment.get("profile") != previous.get("profile")
            ):
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
