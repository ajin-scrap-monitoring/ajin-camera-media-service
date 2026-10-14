import asyncio
import io

import imageio_ffmpeg
import pytest
from aiohttp import ClientSession
from PIL import Image
from websockets.asyncio.client import connect

from camera_media_service.app import create_app
from camera_media_service.settings import Settings


@pytest.mark.asyncio
async def test_real_wss_ingest_and_both_api_versions_run_together(tmp_path):
    settings = Settings(
        camera_tokens={"camera-main": "test"},
        host="127.0.0.1",
        port=0,
        api_port=0,
        media_root=str(tmp_path),
        ffmpeg_path=imageio_ffmpeg.get_ffmpeg_exe(),
        daily_recording_enabled=True,
        max_daily_storage_bytes=100_000_000,
        min_free_storage_bytes=0,
        daily_width=320,
        daily_height=240,
        daily_fps=10,
    )
    app = create_app(settings)
    raw = io.BytesIO()
    Image.new("RGB", (320, 240), "blue").save(raw, "JPEG")
    async with app.serve() as runtime:
        async with ClientSession() as client:
            api = f"http://127.0.0.1:{runtime.api_port}/api"
            assert (
                await client.get(api + "/v1/cameras/camera-main/recording-policy")
            ).status == 200
            assert (
                await client.get(api + "/v2/cameras/camera-main/daily-recording-policy")
            ).status == 200
            host, port = runtime.socket.getsockname()[:2]
            async with connect(
                f"ws://{host}:{port}/api/v1/cameras/camera-main/stream",
                additional_headers={"Authorization": "Bearer test"},
            ) as socket:
                for _ in range(5):
                    await socket.send(raw.getvalue())
                    await asyncio.sleep(0.03)
            result = await (
                await client.get(api + "/v2/cameras/camera-main/daily-recordings")
            ).json()
            assert len(result["items"]) == 1
    # Shutdown flushes the short real input; restarting preserves the day ID and completed TS.
    app = create_app(settings)
    async with app.serve() as runtime:
        async with ClientSession() as client:
            result2 = await (
                await client.get(
                    f"http://127.0.0.1:{runtime.api_port}/api/v2/cameras/camera-main/daily-recordings"
                )
            ).json()
            assert result2["items"][0]["recording_id"] == result["items"][0]["recording_id"]
            assert result2["items"][0]["playback_state"] == "ready"
