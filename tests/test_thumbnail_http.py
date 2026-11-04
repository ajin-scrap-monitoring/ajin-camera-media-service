import asyncio
import shutil
import time

import imageio_ffmpeg
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_media_info import make_video
from test_recording import make_service

from camera_media_service.http_api import create_http_app
from camera_media_service.recording import iso


@pytest.mark.asyncio
async def test_thumbnail_routes_are_separate_no_store_and_actual_jpeg(tmp_path):
    recording = make_service(tmp_path)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    async with TestClient(TestServer(create_http_app(recording))) as client:
        body, _ = await recording.create_clip("camera-main", "http-thumbnail")
        video = make_video(tmp_path)
        key = body["clip_id"]
        shutil.copyfile(video, recording.clips / (key + ".mp4"))
        body.update(
            status="ready", completed_at=iso(time.time()), expires_at=iso(time.time() + 10000)
        )
        async with recording.lock:
            with recording.db:
                recording._save(body)
        base = "/api/v1/clips/" + key
        old = await (await client.get(base + "/media-info")).json()
        assert old["thumbnail"] == {
            "status": "unsupported",
            "representative_at": None,
            "content_path": None,
        }
        response = await client.get(base + "/thumbnail-info")
        assert response.status == 200
        assert (await response.json())["reason_code"] == "NOT_PREPARED"
        assert response.headers["Cache-Control"] == "no-store"
        response = await client.post(base + "/thumbnail", json={})
        assert response.status == 202
        assert response.headers["Location"] == base + "/thumbnail-info"
        for _ in range(200):
            info = await (await client.get(base + "/thumbnail-info")).json()
            if info["status"] != "pending":
                break
            await asyncio.sleep(0.02)
        assert info["status"] == "ready", info
        response = await client.get(info["content_path"])
        data = await response.read()
        assert response.status == 200 and data[:2] == bytes([255, 216])
        assert response.headers["Content-Type"] == "image/jpeg"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert int(response.headers["Content-Length"]) == info["size_bytes"] == len(data)
        response = await client.head(info["content_path"])
        assert response.status == 200 and int(response.headers["Content-Length"]) == len(data)
        assert await response.read() == b""
        assert (await client.delete(base)).status == 204
        for suffix in ("/thumbnail", "/thumbnail-info"):
            response = await client.get(base + suffix)
            assert response.status == 410 and response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_thumbnail_bad_ids_and_body_are_safe_json_errors(tmp_path):
    recording = make_service(tmp_path)
    async with TestClient(TestServer(create_http_app(recording))) as client:
        response = await client.get("/api/v1/clips/not-a-uuid/thumbnail-info")
        assert response.status == 400 and response.headers["Cache-Control"] == "no-store"
        assert (await response.json())["error"]["code"] == "INVALID_ARGUMENT"
        body, _ = await recording.create_clip("camera-main", "not-ready")
        base = "/api/v1/clips/" + body["clip_id"] + "/thumbnail"
        for data in (
            {"path": "../../other.mp4"},
            {"retry_failed": 1},
            {"retry_failed": True, "force": True},
        ):
            assert (await client.post(base, json=data)).status == 400
        response = await client.post(base, json={})
        assert response.status == 200 and (await response.json())["status"] == "unavailable"
