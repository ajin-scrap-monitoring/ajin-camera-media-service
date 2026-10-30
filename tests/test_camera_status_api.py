from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from aiohttp import ClientSession
from test_daily_recording import jpeg
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from camera_media_service.app import create_app
from camera_media_service.settings import Settings


@pytest_asyncio.fixture
async def status_api(tmp_path):
    app = create_app(
        Settings(
            camera_tokens={"cam-1": "secret-token"},
            host="127.0.0.1",
            port=0,
            api_host="127.0.0.1",
            api_port=0,
            media_root=str(tmp_path),
        )
    )
    async with app.serve() as server, ClientSession() as client:
        host, port = server.socket.getsockname()[:2]
        yield (
            app,
            client,
            f"http://127.0.0.1:{server.api_port}/api/v1/cameras/cam-1/status",
            f"ws://{host}:{port}/api/v1/cameras/cam-1/stream",
        )


async def get_status(client, url):
    async with client.get(url) as response:
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        return await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
async def test_status_unknown_before_authentication_and_after_rejected_handshake(
    status_api, authorization
):
    app, client, url, websocket_url = status_api
    before = await get_status(client, url)
    assert before["camera_id"] == "cam-1"
    assert before["status"] == "unknown"
    assert before["connection_state"] == "unknown"
    assert before["reason_code"] == "NO_OBSERVATION"
    assert before["last_frame_received_at"] is None
    assert before["status_basis"] == "accepted_jpeg_receipt"

    headers = {} if authorization is None else {"Authorization": authorization}
    with pytest.raises(InvalidStatus) as exc:
        async with connect(websocket_url, additional_headers=headers, compression=None):
            pass
    assert exc.value.response.status_code == 401

    after = await get_status(client, url)
    assert after["status"] == "unknown"
    assert after["connection_state"] == "unknown"
    assert after["reason_code"] == "NO_OBSERVATION"
    assert after["last_frame_received_at"] is None
    assert app.frame_store.get("cam-1") is None


@pytest.mark.asyncio
async def test_status_tracks_authenticated_socket_valid_receipt_and_disconnect(status_api):
    app, client, url, websocket_url = status_api
    async with connect(
        websocket_url,
        additional_headers={"Authorization": "Bearer secret-token"},
        compression=None,
    ) as websocket:
        awaiting = await get_status(client, url)
        assert awaiting["status"] == "unknown"
        assert awaiting["connection_state"] == "connected"
        assert awaiting["reason_code"] == "AWAITING_FRAME"
        assert awaiting["last_frame_received_at"] is None

        frame = jpeg()
        await websocket.send(frame)
        assert await asyncio.wait_for(app.wait_for_generation("cam-1", 1), 1) == (1, frame)
        healthy = await get_status(client, url)
        assert healthy["status"] == "healthy"
        assert healthy["connection_state"] == "connected"
        assert healthy["reason_code"] == "FRAME_RECEIVED"
        assert healthy["status_basis"] == "accepted_jpeg_receipt"
        assert healthy["last_frame_received_at"].endswith("Z")
        assert healthy["freshness_seconds"] == 5

    async with asyncio.timeout(1):
        while True:
            disconnected = await get_status(client, url)
            if disconnected["connection_state"] == "disconnected":
                break
            await asyncio.sleep(0.01)
    assert disconnected["status"] == "interrupted"
    assert disconnected["reason_code"] == "CONNECTION_CLOSED"
    assert disconnected["last_frame_received_at"] == healthy["last_frame_received_at"]


@pytest.mark.asyncio
async def test_status_unknown_camera_returns_not_found(status_api):
    _, client, url, _ = status_api
    async with client.get(url.replace("cam-1", "unregistered")) as response:
        assert response.status == 404
        body = await response.json()
    assert body["error"]["code"] == "CAMERA_NOT_FOUND"
