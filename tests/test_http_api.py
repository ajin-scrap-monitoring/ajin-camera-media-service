import asyncio
import time

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from camera_media_service import http_api
from camera_media_service.pipeline import LiveFrameHub
from camera_media_service.recording import RecordingService
from camera_media_service.store import LatestFrameStore


@pytest_asyncio.fixture
async def api(tmp_path):
    hub = LiveFrameHub(LatestFrameStore())
    service = RecordingService(tmp_path, {"camera-main"}, hub)
    async with TestClient(TestServer(http_api.create_http_app(service))) as client:
        yield client, service, hub


@pytest.mark.asyncio
async def test_policy_without_auth_validation_and_unknown_camera(api):
    client, _, _ = api
    path = "/api/v1/cameras/camera-main/recording-policy"
    assert (await client.get(path)).status == 200
    response = await client.get(path)
    assert await response.json() == {
        "pre_roll_seconds": 30,
        "post_roll_seconds": 30,
        "retention_days": 30,
    }
    for value in (True, -1, 301, "10"):
        response = await client.put(
            path,
            json={
                "pre_roll_seconds": value,
                "post_roll_seconds": 10,
                "retention_days": 1,
            },
        )
        assert response.status == 400
    response = await client.put(path, data="{}")
    assert response.status == 415
    assert (await client.get(path.replace("camera-main", "missing"))).status == 404


@pytest.mark.asyncio
async def test_event_list_delete_contract_and_cursor_validation(api):
    client, _, _ = api
    response = await client.post(
        "/api/v1/clips",
        json={
            "camera_id": "camera-main",
            "event_id": "one",
        },
    )
    assert response.status == 202
    job = await response.json()
    path = response.headers["Location"]
    duplicate = await client.post(
        "/api/v1/clips",
        json={
            "camera_id": "camera-main",
            "event_id": "one",
        },
    )
    assert duplicate.status == 200
    assert (await duplicate.json())["clip_id"] == job["clip_id"]
    assert (await client.delete(path)).status == 409
    for event in ("two", "three"):
        await client.post(
            "/api/v1/clips",
            json={
                "camera_id": "camera-main",
                "event_id": event,
            },
        )
    first = await (await client.get("/api/v1/clips?limit=2")).json()
    second = await (
        await client.get(
            "/api/v1/clips",
            params={
                "limit": "2",
                "cursor": first["next_cursor"],
            },
        )
    ).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert len({i["clip_id"] for i in first["items"] + second["items"]}) == 3
    assert (await client.get("/api/v1/clips?cursor=broken")).status == 400
    assert (await client.get("/api/v1/clips?from=invalid")).status == 400


@pytest.mark.asyncio
async def test_live_stream_is_jpeg_without_transcoding(api):
    client, _, hub = api
    path = "/api/v1/cameras/camera-main/live"
    assert (await client.get(path)).status == 503
    jpeg = b"\xff\xd8test\xff\xd9"
    hub.publish("camera-main", jpeg)
    response = await client.get(path)
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
    data = await asyncio.wait_for(response.content.readuntil(jpeg), 2)
    assert b"Content-Type: image/jpeg\r\n" in data
    assert b"Content-Length: 8\r\n" in data
    response.close()


@pytest.mark.asyncio
async def test_content_range_delete_and_expiration(api):
    client, service, _ = api
    job, _ = await service.create_clip("camera-main", "file")
    clip_id = job["clip_id"]
    # Fixture representing a previously completed file, to isolate HTTP byte handling.
    from camera_media_service.recording import iso

    job.update(
        status="ready",
        expires_at=iso(time.time() + 100),
        content_path=f"/api/v1/clips/{clip_id}/content",
    )
    with service.db:
        service._save(job)
    (service.clips / f"{clip_id}.mp4").write_bytes(b"0123456789")
    path = f"/api/v1/clips/{clip_id}"
    response = await client.get(path + "/content", headers={"Range": "bytes=2-5"})
    assert response.status == 206
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert await response.read() == b"2345"
    assert (await client.get(path + "/content", headers={"Range": "bytes=100-"})).status == 416
    response = await client.get(path + "/content?download=true")
    assert "attachment" in response.headers["Content-Disposition"]
    assert await response.read() == b"0123456789"
    assert (await client.delete(path)).status == 204
    assert not (service.clips / f"{clip_id}.mp4").exists()
    assert (await client.delete(path)).status == 204
    assert (await client.get(path + "/content")).status == 410
    items = await (await client.get("/api/v1/clips")).json()
    assert items["items"] == []
    detail = await (await client.get(path)).json()
    assert detail["status"] == "deleted" and detail["deleted_at"]


@pytest.mark.asyncio
async def test_unknown_event_fields_and_duplicate_json_rejected(api):
    client, _, _ = api
    response = await client.post(
        "/api/v1/clips",
        json={
            "event_id": "one",
            "camera_id": "camera-main",
            "occurred_at": "now",
        },
    )
    assert response.status == 400
    response = await client.post(
        "/api/v1/clips",
        headers={"Content-Type": "application/json"},
        data='{"event_id":"one","event_id":"two","camera_id":"camera-main"}',
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_expiration_removes_file_but_retains_event_key(api):
    from camera_media_service.recording import iso

    client, service, _ = api
    job, _ = await service.create_clip("camera-main", "expired")
    job.update(status="ready", expires_at=iso(time.time() - 1))
    with service.db:
        service._save(job)
    path = service.clips / (job["clip_id"] + ".mp4")
    path.write_bytes(b"expired")
    response = await client.get(f"/api/v1/clips/{job['clip_id']}/content")
    assert response.status == 410
    assert (await response.json())["error"]["code"] == "CLIP_EXPIRED"
    assert not path.exists()
    response = await client.post(
        "/api/v1/clips",
        json={
            "camera_id": "camera-main",
            "event_id": "expired",
        },
    )
    assert response.status == 200
    assert (await response.json())["status"] == "expired"


@pytest.mark.asyncio
async def test_delete_failure_does_not_claim_deleted(api, monkeypatch):
    client, service, _ = api
    job, _ = await service.create_clip("camera-main", "fail-delete")
    job.update(status="failed")
    with service.db:
        service._save(job)

    async def unavailable(clip_id):
        raise PermissionError("filesystem unavailable")

    monkeypatch.setattr(service, "_remove_files", unavailable)
    response = await client.delete(f"/api/v1/clips/{job['clip_id']}")
    assert response.status == 503
    assert (await service.get_clip(job["clip_id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_cursor_survives_restart_without_shared_token(tmp_path):
    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    async with TestClient(TestServer(http_api.create_http_app(service))) as client:
        for event in ("first", "second"):
            response = await client.post(
                "/api/v1/clips",
                json={
                    "camera_id": "camera-main",
                    "event_id": event,
                },
            )
            assert response.status == 202
        first = await (await client.get("/api/v1/clips?limit=1")).json()
    reopened = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    async with TestClient(TestServer(http_api.create_http_app(reopened))) as client:
        response = await client.get(
            "/api/v1/clips",
            params={
                "limit": "1",
                "cursor": first["next_cursor"],
            },
        )
        assert response.status == 200
        second = await response.json()
        assert len(second["items"]) == 1
        assert second["items"][0]["clip_id"] != first["items"][0]["clip_id"]
