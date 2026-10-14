"""Synthetic real-time network probe. Run only against an isolated test camera/service."""

import argparse
import asyncio
import io
import json
import os
import time
from pathlib import Path

from aiohttp import ClientSession
from PIL import Image, ImageDraw
from websockets.asyncio.client import connect


async def run(args):
    token = os.environ["DAILY_TEST_EDGE_TOKEN"]
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    frames = 0
    jpeg_bytes = 0
    reconnects = 0
    mjpeg_bytes = 0
    samples = []
    event_id = "daily-probe-" + str(time.time_ns())
    event = None
    session_path = f"/api/v1/cameras/{args.camera}/live"
    async with ClientSession() as client:
        response = await client.put(
            args.api + f"/api/v1/cameras/{args.camera}/recording-policy",
            json={"pre_roll_seconds": 3, "post_roll_seconds": 3, "retention_days": 1},
        )
        assert response.status == 200, await response.text()

        async def viewer():
            nonlocal mjpeg_bytes
            async with client.get(args.api + session_path) as response:
                assert response.status == 200
                async for chunk in response.content.iter_chunked(65536):
                    mjpeg_bytes += len(chunk)

        view = asyncio.create_task(viewer())
        next_sample = 0
        next_frame = time.monotonic()
        deliberate_disconnect = False
        try:
            while time.monotonic() - start < args.seconds:
                try:
                    async with connect(
                        args.ingest,
                        additional_headers={"Authorization": "Bearer " + token},
                        compression=None,
                        open_timeout=10,
                    ) as socket:
                        reconnects += 1
                        while time.monotonic() - start < args.seconds:
                            elapsed = time.monotonic() - start
                            if not deliberate_disconnect and elapsed > min(20, args.seconds / 3):
                                deliberate_disconnect = True
                                await socket.close()
                                await asyncio.sleep(1.5)
                                next_frame = time.monotonic()
                                break
                            image = Image.new("RGB", (args.width, args.height), "#557070")
                            draw = ImageDraw.Draw(image)
                            for j in range(20):
                                x = (frames * (j + 1) + j * 91) % max(1, args.width - 100)
                                y = j * 53 % max(1, args.height - 100)
                                draw.rectangle(
                                    (x, y, x + 80, y + 80), fill=(30 + j * 9, 150, 210 - j * 6)
                                )
                            draw.text((20, 20), f"SYNTHETIC {frames} {elapsed:.3f}s", fill="white")
                            raw = io.BytesIO()
                            image.save(raw, "JPEG", quality=85)
                            data = raw.getvalue()
                            await socket.send(data)
                            frames += 1
                            jpeg_bytes += len(data)
                            if event is None and elapsed > min(10, args.seconds / 4):
                                response = await client.post(
                                    args.api + "/api/v1/clips",
                                    json={"camera_id": args.camera, "event_id": event_id},
                                )
                                assert response.status in (200, 202), await response.text()
                                event = await response.json()
                            if elapsed >= next_sample:
                                response = await client.get(
                                    args.api
                                    + f"/api/v2/cameras/{args.camera}/daily-recordings?limit=3"
                                )
                                assert response.status == 200, await response.text()
                                items = (await response.json())["items"]
                                samples.append(
                                    {
                                        "elapsed_seconds": round(elapsed, 3),
                                        "frames": frames,
                                        "days": [
                                            {
                                                k: d[k]
                                                for k in (
                                                    "recording_id",
                                                    "recording_date",
                                                    "status",
                                                    "recorded_seconds",
                                                    "playable_until_at",
                                                    "error",
                                                    "gaps",
                                                )
                                            }
                                            for d in items
                                        ],
                                    }
                                )
                                report.write_text(
                                    json.dumps({"state": "running", "samples": samples}, indent=2),
                                    encoding="utf-8",
                                )
                                next_sample += 60
                            next_frame += 1 / args.fps
                            if next_frame < time.monotonic() - 1:
                                next_frame = time.monotonic()
                            await asyncio.sleep(max(0, next_frame - time.monotonic()))
                except (OSError, TimeoutError) as exc:
                    samples.append({"connection_error": type(exc).__name__})
                    await asyncio.sleep(1)
            await asyncio.sleep(12)
            response = await client.get(
                args.api + f"/api/v2/cameras/{args.camera}/daily-recordings?limit=3"
            )
            days = (await response.json())["items"]
            assert any(d["recorded_seconds"] > 0 for d in days), days
            for day in days:
                if day["playback_path"]:
                    playlist = await (await client.get(args.api + day["playback_path"])).text()
                    assert "#EXT-X-PROGRAM-DATE-TIME" in playlist
            clip = await (await client.get(args.api + "/api/v1/clips/" + event["clip_id"])).json()
            assert clip["status"] == "ready", clip
            assert mjpeg_bytes > 0
            result = {
                "state": "passed",
                "synthetic": True,
                "duration_seconds": args.seconds,
                "input_width": args.width,
                "input_height": args.height,
                "target_fps": args.fps,
                "frames_sent": frames,
                "jpeg_bytes_sent": jpeg_bytes,
                "connections": reconnects,
                "mjpeg_bytes_received": mjpeg_bytes,
                "event_clip": clip,
                "days": days,
                "samples": samples,
            }
            report.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        k: result[k]
                        for k in (
                            "state",
                            "frames_sent",
                            "jpeg_bytes_sent",
                            "connections",
                            "duration_seconds",
                        )
                    }
                )
            )
        finally:
            view.cancel()
            await asyncio.gather(view, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--ingest", required=True)
    parser.add_argument("--camera", default="camera-main")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--report", required=True)
    asyncio.run(run(parser.parse_args()))
