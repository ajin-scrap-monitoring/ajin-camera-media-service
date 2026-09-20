from __future__ import annotations

import asyncio
import re
import ssl
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from camera_media_service.auth import AuthenticationError, BearerAuthenticator
from camera_media_service.delivery import (
    BrowserSessionAuthorizer,
    LiveDeliveryEndpoint,
    SessionRegistry,
)
from camera_media_service.ingest import IngestProtocolError, IngestService
from camera_media_service.pipeline import FrameValidator, LiveFrameHub
from camera_media_service.registry import ConnectionRegistry
from camera_media_service.settings import Settings
from camera_media_service.storage import LocalStorageBackend, RingBufferIndex, SegmentWriter
from camera_media_service.store import LatestFrameStore
from camera_media_service.telemetry import IngestTelemetryTracker, StatsLoggerTask


@dataclass(slots=True)
class ConnectionState:
    camera_id: str
    connection_id: str


class WebSocketServer:
    def __init__(
        self,
        settings: Settings,
        authenticator: BearerAuthenticator,
        registry: ConnectionRegistry,
        frame_store: LatestFrameStore,
        ingest_service: IngestService,
        hub: LiveFrameHub | None = None,
        segment_writer: SegmentWriter | None = None,
        ring_buffer_index: RingBufferIndex | None = None,
        session_registry: SessionRegistry | None = None,
        live_delivery: LiveDeliveryEndpoint | None = None,
        telemetry: IngestTelemetryTracker | None = None,
        stats_task: StatsLoggerTask | None = None,
    ) -> None:
        self.settings = settings
        self.authenticator = authenticator
        self.registry = registry
        self.frame_store = frame_store
        self.ingest_service = ingest_service
        self.hub = hub
        self.segment_writer = segment_writer
        self.ring_buffer_index = ring_buffer_index
        self.session_registry = session_registry
        self.live_delivery = live_delivery
        self.telemetry = telemetry
        self.stats_task = stats_task

        route_pattern = re.escape(settings.route_path).replace(
            re.escape("{camera_id}"),
            "(?P<camera_id>[^/]+)",
        )
        self._route_regex = re.compile(f"^{route_pattern}$")
        self._connection_states: dict[ServerConnection, ConnectionState] = {}

    def _camera_id_from_path(self, path: str) -> str | None:
        match = self._route_regex.fullmatch(path)
        if match is None:
            return None
        return match.group("camera_id")

    async def _process_request(
        self, connection: ServerConnection, request: object
    ) -> object | None:
        path = request.path
        camera_id = self._camera_id_from_path(path)
        if camera_id is None:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not Found\n")

        try:
            self.authenticator.authenticate(camera_id, request.headers.get("Authorization"))
        except AuthenticationError:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

        self._connection_states[connection] = ConnectionState(
            camera_id=camera_id,
            connection_id=uuid.uuid4().hex,
        )
        return None

    async def _handler(self, websocket: ServerConnection) -> None:
        state = self._connection_states.pop(websocket, None)
        if state is None:
            await websocket.close(1008, "unknown camera route")
            return

        try:
            await self.ingest_service.receive(state.camera_id, state.connection_id, websocket)
        except (ConnectionClosed, IngestProtocolError):
            return
        finally:
            self._connection_states.pop(websocket, None)

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.settings.server_cert_path == "":
            return None

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=self.settings.server_cert_path,
            keyfile=self.settings.server_key_path,
        )
        return context

    @asynccontextmanager
    async def serve(self):
        runner = None
        api_port = None
        if self.stats_task is not None:
            self.stats_task.start()

        try:
            from aiohttp import web

            from camera_media_service.http_api import create_http_app
            from camera_media_service.recording import RecordingService

            assert self.hub is not None
            recording = RecordingService(
                Path(self.settings.media_root),
                set(self.settings.camera_tokens),
                self.hub,
                ffmpeg=self.settings.ffmpeg_path,
                max_buffer_bytes=self.settings.max_buffer_bytes,
                max_jobs=self.settings.max_clip_jobs,
            )
            runner = web.AppRunner(
                create_http_app(recording),
                shutdown_timeout=2,
                access_log=None,
            )
            await runner.setup()
            site = web.TCPSite(runner, self.settings.api_host, self.settings.api_port)
            await site.start()
            api_port = runner.addresses[0][1]
            async with serve(
                self._handler,
                host=self.settings.host,
                port=self.settings.port,
                process_request=self._process_request,
                compression=None,
                max_size=self.settings.max_frame_bytes,
                ssl=self._ssl_context(),
            ) as server:
                yield SimpleNamespace(socket=server.sockets[0], server=server, api_port=api_port)
        finally:
            if runner is not None:
                await runner.cleanup()
            if self.stats_task is not None:
                await self.stats_task.stop()
            if self.segment_writer is not None:
                await self.segment_writer.stop()

    async def wait_for_generation(self, camera_id: str, generation: int) -> tuple[int, bytes]:
        while True:
            current = self.frame_store.get(camera_id)
            if current is not None and current[0] >= generation:
                return current
            await asyncio.sleep(0.01)


def create_app(settings: Settings) -> WebSocketServer:
    registry = ConnectionRegistry()
    frame_store = LatestFrameStore()
    authenticator = BearerAuthenticator(settings.camera_tokens)

    validator = FrameValidator(max_frame_bytes=settings.max_frame_bytes)
    hub = LiveFrameHub(frame_store=frame_store)

    storage_backend = LocalStorageBackend(media_root=settings.media_root)
    ring_buffer_index = RingBufferIndex()
    segment_writer = SegmentWriter(
        hub=hub,
        backend=storage_backend,
        index=ring_buffer_index,
    )

    browser_authorizer = BrowserSessionAuthorizer()
    session_registry = SessionRegistry()
    live_delivery = LiveDeliveryEndpoint(
        hub=hub,
        authorizer=browser_authorizer,
        session_registry=session_registry,
    )

    telemetry = IngestTelemetryTracker()
    stats_task = StatsLoggerTask(
        telemetry=telemetry,
        interval_seconds=settings.stats_interval_seconds,
    )

    ingest_service = IngestService(
        registry=registry,
        frame_store=frame_store,
        max_frame_bytes=settings.max_frame_bytes,
        validator=validator,
        hub=hub,
        telemetry=telemetry,
    )

    return WebSocketServer(
        settings=settings,
        authenticator=authenticator,
        registry=registry,
        frame_store=frame_store,
        ingest_service=ingest_service,
        hub=hub,
        segment_writer=segment_writer,
        ring_buffer_index=ring_buffer_index,
        session_registry=session_registry,
        live_delivery=live_delivery,
        telemetry=telemetry,
        stats_task=stats_task,
    )
