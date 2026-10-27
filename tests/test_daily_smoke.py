"""Real HTTP/WebSocket/FFmpeg with a test clock; this is not a 24-hour soak."""

import asyncio
import io
from datetime import date

import imageio_ffmpeg
import pytest
from aiohttp import ClientSession
from PIL import Image
from websockets.asyncio.client import connect

from camera_media_service.app import create_app
from camera_media_service.daily_models import DailyWindow
from camera_media_service.encoding import probe_media
from camera_media_service.settings import Settings


@pytest.mark.asyncio
async def test_network_midnight_gap_export_restart_delete(tmp_path, monkeypatch):
    import time

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at - 10]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    settings = Settings(
        camera_tokens={"camera-main": "synthetic-only"},
        host="127.0.0.1",
        port=0,
        api_port=0,
        media_root=str(tmp_path),
        ffmpeg_path=ffmpeg,
        daily_recording_enabled=True,
        max_daily_storage_bytes=100_000_000,
        min_free_storage_bytes=0,
        daily_width=320,
        daily_height=240,
        daily_fps=10,
    )
    raw = io.BytesIO()
    Image.new("RGB", (320, 240), "blue").save(raw, "JPEG")
    async with create_app(settings).serve() as runtime, ClientSession() as client:
        base = f"http://127.0.0.1:{runtime.api_port}/api"
        host, port = runtime.socket.getsockname()[:2]
        for start in (window.end_at - 9, window.end_at - 3):
            clock[0] = start
            async with connect(
                f"ws://{host}:{port}/api/v1/cameras/camera-main/stream",
                additional_headers={"Authorization": "Bearer synthetic-only"},
            ) as socket:
                for i in range(10):
                    clock[0] = start + i / 10
                    await socket.send(raw.getvalue())
                    await asyncio.sleep(0.003)
                clock[0] = start + 1
            # Wait for the server to observe the intentional disconnection before advancing time.
            await asyncio.sleep(0.03)
        clock[0] = window.end_at + 10
        for _ in range(200):
            response = await client.get(base + "/v2/cameras/camera-main/daily-recordings")
            items = (await response.json())["items"]
            if len(items) == 2 and items[1]["status"] == "closed":
                break
            await asyncio.sleep(0.02)
        assert len(items) == 2 and items[1]["status"] == "closed"
        day = items[1]
        assert day["coverage"] == "partial" and 1.9 <= day["recorded_seconds"] <= 2.1
        assert any(g["reason_code"] == "CAMERA_DISCONNECTED" for g in day["gaps"])
        detail = base + "/v2/daily-recordings/" + day["recording_id"]
        playlist = await (await client.get(base[:-4] + day["playback_path"])).text()
        assert "#EXT-X-ENDLIST" in playlist and "#EXT-X-DISCONTINUITY" in playlist
        paths = [line for line in playlist.splitlines() if line.startswith("/api/")]
        for path in paths:
            response = await client.get(base[:-4] + path, headers={"Range": "bytes=0-187"})
            assert response.status == 206 and len(await response.read()) == 188
        assert (await client.post(detail + "/exports", json={})).status in (200, 202)
        # The automatic worker polls every five seconds; POST only reads status.
        for _ in range(1000):
            result = await (await client.get(detail)).json()
            if result["export_state"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.02)
        assert result["export_state"] == "ready", result
        response = await client.get(detail + "/content?download=true")
        assert response.status == 200 and "attachment" in response.headers["Content-Disposition"]
        saved = tmp_path / "download.mp4"
        saved.write_bytes(await response.read())
        metadata = await probe_media(ffmpeg, saved)
        assert abs(metadata["duration"] - day["recorded_seconds"]) < 0.1
        today = base + "/v2/daily-recordings/" + items[0]["recording_id"]
        assert (await client.post(today + "/exports", json={})).status == 409
    async with create_app(settings).serve() as runtime, ClientSession() as client:
        detail = (
            f"http://127.0.0.1:{runtime.api_port}/api/v2/daily-recordings/{day['recording_id']}"
        )
        assert (await (await client.get(detail)).json())["export_state"] == "ready"
        response = await client.get(detail + "/content", headers={"Range": "bytes=0-99"})
        assert response.status == 206 and len(await response.read()) == 100
        assert (await client.delete(detail)).status == 204
        assert (await client.delete(detail)).status == 204
        assert (await client.get(detail + "/content")).status == 410
