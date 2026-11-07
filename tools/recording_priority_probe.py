"""Opt-in isolated probe: real-paced JPEG/WS, accelerated midnight and real archival.

Requires dev dependencies. Never point --root at an existing service store.
The clock is offset inside this process only; this is not a 24-hour soak test.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import io
import json
import os
import shutil
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, ImageDraw
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from camera_media_service import http_api, ingest, persisted_media_info, pipeline, recording
from camera_media_service.daily_catalog import DailyCatalog
from camera_media_service.daily_export import DailyExportService
from camera_media_service.daily_models import DailyWindow
from camera_media_service.daily_recording import DailyRecordingService
from camera_media_service.encoding import probe_media, start_media_process
from camera_media_service.http_api import create_http_app
from camera_media_service.registry import ConnectionRegistry
from camera_media_service.store import LatestFrameStore


def images():
    result = []
    for phase in range(12):
        frame = Image.new("RGB", (1920, 1080))
        draw = ImageDraw.Draw(frame)
        for y in range(0, 1080, 48):
            for x in range(0, 1920, 64):
                draw.rectangle(
                    (x, y, x + 63, y + 47),
                    fill=((x + y) % 256, (x * 3 + y) % 256, (y * 7 + x) % 256),
                )
        draw.rectangle((phase * 120, 300, phase * 120 + 250, 600), fill="yellow")
        output = io.BytesIO()
        frame.save(output, "JPEG", quality=80)
        result.append(output.getvalue())
    return result


async def run(args):
    root = args.root.resolve()
    if not root.name.startswith("priority-probe-") or (root.exists() and any(root.iterdir())):
        raise RuntimeError("Use a new, isolated priority-probe-* directory")
    root.mkdir(parents=True, exist_ok=True)
    frames = images()
    store = LatestFrameStore()
    hub = pipeline.LiveFrameHub(store)
    owner = recording.RecordingService(root, {"camera-main"}, hub, ffmpeg=args.ffmpeg)
    settings = SimpleNamespace(
        daily_segment_seconds=10,
        max_daily_storage_bytes=6 * 1024**3,
        max_media_storage_bytes=8 * 1024**3,
        min_free_storage_bytes=0,
        max_daily_pending_seconds=30,
        daily_width=1920,
        daily_height=1080,
        daily_fps=15,
        daily_archive_enabled=True,
        daily_archive_workspace_bytes=512 * 1024**2,
    )
    window = DailyWindow.from_date(date(2026, 11, 7))
    anchor = window.start_at - 30
    began = time.monotonic()
    frozen = [None]

    def clock():
        return frozen[0] if frozen[0] is not None else anchor + time.monotonic() - began

    daily = DailyRecordingService(owner, DailyCatalog(owner.db), clock=clock, settings=settings)
    seed = root / "seed-source.ts"
    child = await start_media_process(
        args.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=15",
        "-t",
        "10",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "2",
        "-crf",
        "30",
        "-f",
        "mpegts",
        str(seed),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await child.communicate()
    assert child.returncode == 0, error[-1000:]
    measurement = await probe_media(args.ffmpeg, seed, full_decode=False)
    previous = DailyWindow.from_date(date(2026, 11, 6))
    with owner.db:
        old = daily._ensure_day("camera-main", previous, 30)
        for index in range(48):
            start = previous.start_at + index * 10
            segment = daily.catalog.begin_segment(old["recording_id"], start, start + 10)
            path = daily.segment_path(segment)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(seed, path)
            daily.catalog.complete_segment(
                segment["segment_id"],
                {
                    **measurement,
                    "path": str(path.relative_to(root)),
                    "profile": daily.profile,
                    "start_at": start,
                    "end_at": start + 10,
                },
                persist_measurement=False,
            )
        daily._refresh(old, anchor)
        owner.db.execute("INSERT INTO daily_cursors VALUES(?,?)", ("camera-main", anchor))
    owner.daily_frame_watermarks["camera-main"] = anchor
    exports = DailyExportService(daily, settings)
    began = time.monotonic()
    proxy = SimpleNamespace(time=clock, monotonic=time.monotonic)
    # Do not mutate the global time module: the independent producer and HTTP
    # libraries must retain real wall/monotonic time for pacing and deadlines.
    saved_times = pipeline.time, recording.time, ingest.time, http_api.time
    pipeline.time, recording.time, ingest.time, http_api.time = proxy, proxy, proxy, proxy
    original_write = persisted_media_info._optional_write
    original_store = owner.store_frames
    injected = []

    def slow_metadata(*a, **kw):
        time.sleep(1.2)
        return original_write(*a, **kw)

    async def delayed_write(batch):
        if not injected and time.monotonic() - began > 55:
            injected.append(True)
            await asyncio.sleep(5)
        await original_store(batch)

    persisted_media_info._optional_write = slow_metadata
    owner.store_frames = delayed_write
    receiver = ingest.IngestService(ConnectionRegistry(), store, 4 * 1024**2, hub=hub)
    reports = []
    original_report = owner.diagnostics.report_if_due

    def report():
        if time.monotonic() - owner.diagnostics.last_report >= 60:
            reports.append(owner.diagnostics.snapshot())
        original_report()

    owner.diagnostics.report_if_due = report
    delivered = []

    async def receive(websocket):
        await receiver.receive("camera-main", "priority-probe", websocket)

    async def produce(url):
        async with connect(url, compression=None, max_size=4 * 1024**2) as websocket:
            start = time.monotonic()
            for index in range(args.seconds * 30):
                await websocket.send(frames[index % len(frames)])
                delivered.append(index)
                await asyncio.sleep(max(0, start + (index + 1) / 30 - time.monotonic()))

    async def consume(client):
        total = 0
        while True:
            async with client.get("/api/v1/cameras/camera-main/live") as response:
                if response.status == 503:
                    await response.read()
                    await asyncio.sleep(0.1)
                    continue
                assert response.status == 200
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    live_bytes[0] = total

    live_bytes = [0]
    producer = viewer = None
    result = None
    completed = False
    try:
        async with TestClient(
            TestServer(create_http_app(owner, daily=daily, exports=exports))
        ) as client:
            assert (
                await client.put(
                    "/api/v1/cameras/camera-main/recording-policy",
                    json={"pre_roll_seconds": 2, "post_roll_seconds": 2, "retention_days": 2},
                )
            ).status == 200
            async with serve(
                receive, "127.0.0.1", 0, compression=None, max_size=4 * 1024**2
            ) as websocket:
                url = "ws://127.0.0.1:" + str(websocket.sockets[0].getsockname()[1])
                producer = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(produce(url))))
                viewer = asyncio.create_task(consume(client))
                await asyncio.sleep(85)
                event = await client.post(
                    "/api/v1/clips",
                    json={
                        "camera_id": "camera-main",
                        "event_id": "isolated-priority-probe",
                        "event_at": recording.iso(clock() - 20),
                    },
                )
                assert event.status == 202, await event.text()
                event_id = (await event.json())["clip_id"]
                await producer
                frozen[0] = clock()
                await asyncio.wait_for(owner.queue.wait_received(), 60)
                await daily.tick(clock())
                deadline = time.monotonic() + 120
                while True:
                    old_state = await daily.get_day(old["recording_id"])
                    if old_state["export_state"] == "ready":
                        break
                    assert old_state["export_state"] != "failed", old_state.get("error")
                    assert time.monotonic() < deadline, (
                        "Archive did not finish during bounded settling"
                    )
                    await asyncio.sleep(0.2)
                current = (await daily.list_days("camera-main", {}, 5, None))[0]
                key = current["recording_id"]
                assert key != old["recording_id"]
                image = await (
                    await client.get(f"/api/v2/daily-recordings/{key}/thumbnail-info")
                ).json()
                assert image["status"] == "ready", image
                assert current["archive_state"] == "segments"
                picture = await client.get(image["content_path"])
                assert picture.status == 200 and (await picture.read()).startswith(b"\xff\xd8")
                clip = await (await client.get("/api/v1/clips/" + event_id)).json()
                assert clip["status"] == "ready", clip
                assert (
                    await client.get(
                        "/api/v1/clips/" + event_id + "/content", headers={"Range": "bytes=0-1023"}
                    )
                ).status == 206
                assert (
                    await client.get(
                        f"/api/v2/daily-recordings/{old['recording_id']}/content",
                        headers={"Range": "bytes=0-1023"},
                    )
                ).status == 206
                low = anchor + 2
                high = owner.daily_frame_watermarks["camera-main"]
                rows = owner.db.execute(
                    "SELECT timestamp FROM frames WHERE timestamp>=? AND timestamp<? "
                    "ORDER BY timestamp",
                    (low, high),
                ).fetchall()
                segments = owner.db.execute(
                    "SELECT start,end FROM daily_segments WHERE status='ready' AND start<? "
                    "AND end>? ORDER BY start",
                    (high, low),
                ).fetchall()
                starts = [row[0] for row in segments]
                missing = [
                    at
                    for (at,) in rows
                    if (index := bisect.bisect_right(starts, at) - 1) < 0
                    or at >= segments[index][1]
                ]
                reports.append(owner.diagnostics.snapshot())
                counts = {}
                for stats in reports:
                    for cause, count in stats["loss_counts"].items():
                        counts[cause] = counts.get(cause, 0) + count
                assert len(delivered) == args.seconds * 30 and len(rows) > 3000
                assert not missing
                assert not counts.get("ingest_queue_full", 0)
                if os.name != "nt":
                    assert counts.get("background_process_paused", 0) > 0
                assert injected
                if viewer.done():
                    viewer.result()
                assert live_bytes[0] > 1024**2
                result = {
                    "state": "passed",
                    "real_paced_seconds": args.seconds,
                    "controlled_midnight": True,
                    "prior_fixture_media_seconds": 480,
                    "prior_archive_ready": True,
                    "active_date_jpeg": image,
                    "jpeg_frames_sent": len(delivered),
                    "jpeg_mean_bytes": sum(map(len, frames)) / len(frames),
                    "durable_frames_checked": len(rows),
                    "unrepresented_frames": len(missing),
                    "mjpeg_bytes": live_bytes[0],
                    "event_mp4_range": 206,
                    "daily_mp4_range": 206,
                    "injected_write_stall_seconds": 5,
                    "injected_metadata_stall_seconds": 1.2,
                    "counts": counts,
                    "diagnostics": reports,
                    "is_24_hour_validation": False,
                }
                completed = True
            viewer.cancel()
            await asyncio.gather(viewer, return_exceptions=True)
    finally:
        if viewer:
            viewer.cancel()
            await asyncio.gather(viewer, return_exceptions=True)
        persisted_media_info._optional_write = original_write
        pipeline.time, recording.time, ingest.time, http_api.time = saved_times
        if result:
            result["state"] = "passed" if completed else "failed"
            args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert result
    print(json.dumps({k: v for k, v in result.items() if k != "diagnostics"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--seconds", type=int, default=180)
    options = parser.parse_args()
    if not 180 <= options.seconds <= 600:
        parser.error("seconds must be180..600")
    asyncio.run(run(options))
