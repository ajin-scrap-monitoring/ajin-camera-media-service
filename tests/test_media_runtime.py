import asyncio
import io
import time

import imageio_ffmpeg
import pytest
from aiohttp import ClientSession
from PIL import Image
from websockets.asyncio.client import connect

from camera_media_service.app import create_app
from camera_media_service.settings import Settings


@pytest.mark.asyncio
async def test_wss_ingest_to_event_mp4_and_http_deletion(tmp_path):
    settings = Settings(
        camera_tokens={"camera-main": "edge-secret"},
        host="127.0.0.1",
        port=0,
        media_root=str(tmp_path),
        api_port=0,
        ffmpeg_path=imageio_ffmpeg.get_ffmpeg_exe(),
    )
    app = create_app(settings)
    jpeg = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(jpeg, "JPEG")
    async with app.serve() as server, ClientSession() as client:
        port = server.socket.getsockname()[1]
        base = f"http://127.0.0.1:{server.api_port}/api/v1"
        response = await client.put(
            base + "/cameras/camera-main/recording-policy",
            json={
                "pre_roll_seconds": 1,
                "post_roll_seconds": 1,
                "retention_days": 1,
            },
        )
        assert response.status == 200
        async with connect(
            f"ws://127.0.0.1:{port}/api/v1/cameras/camera-main/stream",
            additional_headers={"Authorization": "Bearer edge-secret"},
        ) as ws:
            for _ in range(4):
                await ws.send(jpeg.getvalue())
                await asyncio.sleep(0.05)
            response = await client.post(
                base + "/clips",
                json={
                    "event_id": "integration",
                    "camera_id": "camera-main",
                },
            )
            assert response.status == 202
            job = await response.json()
            for _ in range(24):
                await ws.send(jpeg.getvalue())
                await asyncio.sleep(0.05)
        deadline = time.monotonic() + 15
        while True:
            response = await client.get(base + "/clips/" + job["clip_id"])
            detail = await response.json()
            if detail["status"] not in ("collecting", "processing"):
                break
            assert time.monotonic() < deadline, detail
            await asyncio.sleep(0.1)
        assert detail["status"] == "ready", detail
        response = await client.get(base + "/clips/" + job["clip_id"] + "/content")
        content = await response.read()
        assert response.status == 200
        assert b"ftyp" in content[:32]
        assert content.index(b"moov") < content.index(b"mdat")
        # Verify the emitted file is actually decodable, not just an MP4 header.
        reader = imageio_ffmpeg.read_frames(str(tmp_path / "clips" / (job["clip_id"] + ".mp4")))
        metadata = next(reader)
        assert metadata["codec"] == "h264"
        assert metadata["size"] == (64, 48)
        assert len(next(reader)) == 64 * 48 * 3
        reader.close()
        response = await client.delete(base + "/clips/" + job["clip_id"])
        assert response.status == 204
        response = await client.get(base + "/clips/" + job["clip_id"] + "/content")
        assert response.status == 410
