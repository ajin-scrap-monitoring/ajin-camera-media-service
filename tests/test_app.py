from __future__ import annotations

import asyncio
import ssl
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import camera_media_service.app as app_module
from camera_media_service.app import create_app
from camera_media_service.settings import Settings

VALID_JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"


class FakeServer:
    sockets = [object()]


class FakeSslContext:
    def __init__(self, protocol: int) -> None:
        self.protocol = protocol
        self.loaded_chain: tuple[str, str] | None = None

    def load_cert_chain(self, certfile: str, keyfile: str) -> None:
        self.loaded_chain = (certfile, keyfile)


@pytest.mark.asyncio
async def test_create_app_exposes_camera_stream_route_and_ingests_valid_jpeg() -> None:
    settings = Settings(camera_tokens={"cam-1": "secret-token"}, host="127.0.0.1", port=0)
    app = create_app(settings)

    async with app.serve() as server:
        host, port = server.socket.getsockname()[:2]
        uri = f"ws://{host}:{port}/api/v1/cameras/cam-1/stream"

        async with connect(
            uri,
            additional_headers={"Authorization": "Bearer secret-token"},
            compression=None,
        ) as websocket:
            await websocket.send(VALID_JPEG)

        await asyncio.wait_for(app.wait_for_generation("cam-1", 1), timeout=1)
        assert app.frame_store.get("cam-1") == (1, VALID_JPEG)


@pytest.mark.asyncio
async def test_create_app_rejects_unauthorized_handshake_before_ingest() -> None:
    settings = Settings(camera_tokens={"cam-1": "secret-token"}, host="127.0.0.1", port=0)
    app = create_app(settings)

    async with app.serve() as server:
        host, port = server.socket.getsockname()[:2]
        uri = f"ws://{host}:{port}/api/v1/cameras/cam-1/stream"

        with pytest.raises(InvalidStatus) as exc_info:
            async with connect(uri, compression=None):
                pass

        assert exc_info.value.response.status_code == 401
        assert app.frame_store.get("cam-1") is None


@pytest.mark.asyncio
async def test_create_app_rejects_route_mismatch_before_ingest() -> None:
    settings = Settings(camera_tokens={"cam-1": "secret-token"}, host="127.0.0.1", port=0)
    app = create_app(settings)

    async with app.serve() as server:
        host, port = server.socket.getsockname()[:2]
        uri = f"ws://{host}:{port}/api/v1/cameras/cam-1/not-stream"

        with pytest.raises(InvalidStatus) as exc_info:
            async with connect(
                uri,
                additional_headers={"Authorization": "Bearer secret-token"},
                compression=None,
            ):
                pass

        assert exc_info.value.response.status_code == 404
        assert app.frame_store.get("cam-1") is None


@pytest.mark.asyncio
async def test_create_app_accepts_a_jpeg_larger_than_the_websockets_default_limit() -> None:
    frame = b"\xff\xd8" + (b"a" * (1024 * 1024)) + b"\xff\xd9"
    settings = Settings(camera_tokens={"cam-1": "secret-token"}, host="127.0.0.1", port=0)
    app = create_app(settings)

    async with app.serve() as server:
        host, port = server.socket.getsockname()[:2]
        uri = f"ws://{host}:{port}/api/v1/cameras/cam-1/stream"

        async with connect(
            uri,
            additional_headers={"Authorization": "Bearer secret-token"},
            compression=None,
        ) as websocket:
            await websocket.send(frame)

        assert await asyncio.wait_for(app.wait_for_generation("cam-1", 1), timeout=1) == (1, frame)


@pytest.mark.asyncio
async def test_create_app_rejects_a_frame_above_the_configured_limit() -> None:
    frame = b"\xff\xd8" + (b"a" * (4 * 1024 * 1024)) + b"\xff\xd9"
    settings = Settings(camera_tokens={"cam-1": "secret-token"}, host="127.0.0.1", port=0)
    app = create_app(settings)

    async with app.serve() as server:
        host, port = server.socket.getsockname()[:2]
        uri = f"ws://{host}:{port}/api/v1/cameras/cam-1/stream"

        async with connect(
            uri,
            additional_headers={"Authorization": "Bearer secret-token"},
            compression=None,
        ) as websocket:
            await websocket.send(frame)
            with pytest.raises(ConnectionClosed) as exc_info:
                await websocket.recv()

    assert exc_info.value.rcvd.code == 1009
    assert app.frame_store.get("cam-1") is None


@pytest.mark.asyncio
async def test_websocket_server_builds_tls_context_and_passes_frame_limit_to_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, object] = {}

    @asynccontextmanager
    async def fake_serve(*args: object, **kwargs: object):
        served.update(kwargs)
        yield FakeServer()

    monkeypatch.setattr(app_module, "serve", fake_serve)
    monkeypatch.setattr(app_module.ssl, "SSLContext", FakeSslContext)
    settings = Settings(
        camera_tokens={},
        server_cert_path="/run/secrets/cert.pem",
        server_key_path="/run/secrets/key.pem",
    )

    async with create_app(settings).serve():
        pass

    context = served["ssl"]
    assert isinstance(context, FakeSslContext)
    assert context.protocol == ssl.PROTOCOL_TLS_SERVER
    assert context.loaded_chain == ("/run/secrets/cert.pem", "/run/secrets/key.pem")
    assert served["max_size"] == 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_websocket_server_uses_internal_reverse_proxy_mode_without_tls_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, object] = {}

    @asynccontextmanager
    async def fake_serve(*args: object, **kwargs: object):
        served.update(kwargs)
        yield FakeServer()

    monkeypatch.setattr(app_module, "serve", fake_serve)

    async with create_app(Settings(camera_tokens={})).serve():
        pass

    assert served["ssl"] is None
