"""Read-only, bounded complete-JPEG, durable-receipt and cold/warm HLS diagnostic.

Run inside the media container/network; uses only the Python standard library.
The first HLS request is an unprimed request, not an OS page-cache flush.
No API writes, service startup, database mutation, or authentication data output.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sqlite3
import time
import urllib.request
import uuid
from pathlib import Path


def read_mjpeg_frame(stream, *, max_bytes=16 * 1024**2):
    headers = {}
    for _ in range(64):
        line = stream.readline(8193)
        if not line:
            if headers:
                raise ValueError("Truncated multipart headers")
            return None
        if len(line) > 8192:
            raise ValueError("Oversized multipart header")
        if line in (b"\r\n", b"\n") and headers:
            break
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.lower()] = value.strip()
    else:
        raise ValueError("Multipart headers exceeded bound")
    length = int(headers.get(b"content-length", b"0"))
    if not 4 <= length <= max_bytes:
        raise ValueError("Invalid JPEG length")
    jpeg = stream.read(length)
    if len(jpeg) != length or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("Incomplete JPEG")
    return length


def summarize(times):
    intervals = sorted(b - a for a, b in zip(times, times[1:], strict=False))

    def percentile(fraction):
        return (
            intervals[min(len(intervals) - 1, int((len(intervals) - 1) * fraction))]
            if intervals
            else None
        )

    return {
        "count": len(times),
        "first_at": times[0] if times else None,
        "last_at": times[-1] if times else None,
        "max_interval_s": max(intervals, default=None),
        "p50_interval_s": percentile(0.5),
        "p95_interval_s": percentile(0.95),
        "p99_interval_s": percentile(0.99),
        "over1s": sum(d > 1 for d in intervals),
        "largest_intervals_s": intervals[-8:][::-1],
    }


def sample_mjpeg(url, seconds):
    times, sizes = [], []
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            until = time.monotonic() + seconds
            while time.monotonic() < until and len(times) < 20000:
                size = read_mjpeg_frame(response)
                if size is None:
                    break
                times.append(time.time())
                sizes.append(size)
        return {**summarize(times), "jpeg_bytes": sum(sizes)}
    except (OSError, ValueError) as exc:
        return {**summarize(times), "error_type": type(exc).__name__}


def timed_get(url):
    start = time.monotonic()
    with urllib.request.urlopen(url, timeout=15) as response:
        headers = time.monotonic()
        data = response.read(16 * 1024**2 + 1)
    if len(data) > 16 * 1024**2:
        raise ValueError("Response exceeded diagnostic bound")
    return {
        "headers_s": headers - start,
        "total_s": time.monotonic() - start,
        "bytes": len(data),
    }, data


def sample_hls(base, recording_id):
    samples = []
    for _ in range(2):
        playlist, body = timed_get(
            base + "/api/v2/daily-recordings/" + recording_id + "/hls/index.m3u8"
        )
        paths = [line for line in body.decode().splitlines() if line.startswith("/api/v2/")]
        if not paths:
            raise ValueError("No HLS media")
        index = len(paths) // 2
        segment, _ = timed_get(base + paths[index])
        samples.append(
            {
                "playlist": playlist,
                "segment": segment,
                "segment_index": index,
                "segment_count": len(paths),
            }
        )
    return samples


def durable_snapshot(path, camera, begin, end, days):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        receipts = [
            row[0]
            for row in db.execute(
                "SELECT timestamp FROM frames WHERE camera=? AND timestamp>=? "
                "AND timestamp<=? ORDER BY timestamp",
                (camera, begin, end),
            )
        ]
        result = {"receipts": summarize(receipts), "days": {}}
        for recording_id in days:
            row = db.execute(
                "SELECT body FROM daily_recordings WHERE id=?", (recording_id,)
            ).fetchone()
            if row is None:
                continue
            day = json.loads(row[0])
            segments = list(
                db.execute(
                    "SELECT start,end FROM daily_segments WHERE day_id=? AND status='ready' "
                    "ORDER BY start,id",
                    (recording_id,),
                )
            )
            holes = [
                (left[1], right[0], right[0] - left[1])
                for left, right in zip(segments, segments[1:], strict=False)
                if right[0] > left[1]
            ]
            recent = [segment for segment in segments if begin <= segment[0] <= end]
            recent_holes = [
                (left[1], right[0], right[0] - left[1])
                for left, right in zip(recent, recent[1:], strict=False)
                if right[0] > left[1]
            ]
            reasons = {}
            for gap in day["gaps"]:
                reason = gap["reason_code"]
                reasons[reason] = reasons.get(reason, 0) + 1
            result["days"][recording_id] = {
                "date": day["recording_date"],
                "ready_segments": len(segments),
                "reported_gaps": len(day["gaps"]),
                "gap_reasons": reasons,
                "positive_segment_holes": len(holes),
                "holes_over1s": sum(h[2] > 1 for h in holes),
                "largest_holes": sorted(holes, key=lambda h: h[2], reverse=True)[:8],
                "first_holes": holes[:8],
                "window_ready_segments": len(recent),
                "window_boundary_pairs": max(0, len(recent) - 1),
                "window_positive_holes": len(recent_holes),
                "window_holes": recent_holes[:8],
                "window_segment_spans": recent[-8:],
            }
        return result


def run(args):
    begin = time.time()
    result = {"started_at": begin, "requested_seconds": args.seconds, "hls": {}}
    base = args.api.rstrip("/")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        live = pool.submit(
            sample_mjpeg, base + "/api/v1/cameras/" + args.camera + "/live", args.seconds
        )
        source = (
            pool.submit(sample_mjpeg, args.source_mjpeg, args.seconds)
            if args.source_mjpeg
            else None
        )
        for recording_id in args.day:
            try:
                result["hls"][recording_id] = sample_hls(base, recording_id)
            except (OSError, ValueError) as exc:
                result["hls"][recording_id] = {"error_type": type(exc).__name__}
        result["mjpeg"] = live.result()
        if source:
            result["source_mjpeg"] = source.result()
    result["finished_at"] = time.time()
    if args.sqlite:
        result["durable"] = durable_snapshot(
            args.sqlite, args.camera, begin, result["finished_at"], args.day
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8889")
    parser.add_argument("--camera", default="camera-main")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--day", action="append", default=[])
    parser.add_argument("--sqlite")
    parser.add_argument("--source-mjpeg", help="Optional independent upstream MJPEG endpoint")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or len(args.day) > 7:
        parser.error("seconds must be 1..120; at most seven days")
    try:
        for value in args.day:
            uuid.UUID(value)
    except ValueError:
        parser.error("day must be a UUID")
    print(json.dumps(run(args), indent=2))


if __name__ == "__main__":
    main()
