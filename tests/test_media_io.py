import asyncio
import threading
from pathlib import Path

import pytest
from test_archive_playback import seeded, shutdown

from camera_media_service import media_files


@pytest.mark.asyncio
async def test_media_open_and_stat_keep_event_loop_responsive(tmp_path, monkeypatch):
    path = tmp_path / "media.mp4"
    path.write_bytes(b"abcdefgh")
    main = threading.get_ident()
    real = Path.open

    def open_file(self, *args, **kwargs):
        assert threading.get_ident() != main, "media open blocked shared event loop"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    opener = getattr(media_files, "open_media_file", None)
    assert opener is not None, "async media open helper missing"
    file, size = await opener(path, root=tmp_path)
    try:
        assert size == 8
        assert await asyncio.to_thread(file.read) == b"abcdefgh"
    finally:
        file.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancellation_waits_for_file_operation_before_releasing_owner(cancellations):
    operation = getattr(media_files, "storage_io", None)
    assert operation is not None, "cancellation-safe file worker missing"
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(2)

    task = asyncio.create_task(operation(blocked))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    for _ in range(cancellations):
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_archived_playback_filesystem_operations_leave_event_loop(tmp_path, monkeypatch):
    service, daily, playback, day, parts, _ = await seeded(tmp_path, monkeypatch)
    main = threading.get_ident()
    real_open, real_resolve = Path.open, Path.resolve

    def offloop_open(self, *args, **kwargs):
        assert threading.get_ident() != main, "archive open/write blocked shared event loop"
        return real_open(self, *args, **kwargs)

    def offloop_resolve(self, *args, **kwargs):
        assert threading.get_ident() != main, "archive path metadata blocked shared event loop"
        return real_resolve(self, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", offloop_open)
            patch.setattr(Path, "resolve", offloop_resolve)
            file, size = await playback.open_segment(day["recording_id"], parts[0]["segment_id"])
            assert size > 0
            file.close()
            file, size = await playback.open_segment(day["recording_id"], parts[0]["segment_id"])
            file.close()
            await playback.maintenance(daily.clock())
        assert not daily.storage_reservations
        assert not daily.storage_reservation_outputs
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_daily_http_playlist_and_file_open_leave_event_loop(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from test_daily_export import make_exports, recorded_day

    import camera_media_service.daily_http_api as routes
    from camera_media_service.http_api import create_http_app

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def no_background():
        pass

    monkeypatch.setattr(exports, "start", no_background)
    main = threading.get_ident()
    real_playlist = routes.build_daily_playlist
    real_open = Path.open

    def playlist(*args):
        assert threading.get_ident() != main, "large HLS playlist blocked shared event loop"
        return real_playlist(*args)

    def opened(self, *args, **kwargs):
        if args and args[0] == "rb" and self.suffix in (".mp4", ".ts"):
            assert threading.get_ident() != main, "HTTP file open blocked shared event loop"
        return real_open(self, *args, **kwargs)

    async with TestClient(
        TestServer(create_http_app(service, daily=daily, exports=exports))
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(routes, "build_daily_playlist", playlist)
            patch.setattr(Path, "open", opened)
            response = await client.get(
                "/api/v2/daily-recordings/" + old["recording_id"] + "/hls/index.m3u8"
            )
            assert response.status == 200, await response.text()
            segment = next(p for p in (await response.text()).splitlines() if p.startswith("/api/"))
            response = await client.get(segment, headers={"Range": "bytes=0-31"})
            assert response.status == 206, await response.text()
            assert len(await response.read()) == 32


@pytest.mark.asyncio
async def test_daily_detail_serializes_large_catalog_off_event_loop(tmp_path, monkeypatch):
    import json

    from aiohttp.test_utils import TestClient, TestServer
    from test_daily_export import make_exports, recorded_day

    from camera_media_service.http_api import create_http_app

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)

    async def no_background():
        pass

    monkeypatch.setattr(exports, "start", no_background)
    real = json.dumps
    main = threading.get_ident()

    def dumps(body, *args, **kwargs):
        if isinstance(body, dict) and "available_intervals" in body:
            assert threading.get_ident() != main, "large daily response serialized on event loop"
        return real(body, *args, **kwargs)

    async with TestClient(
        TestServer(create_http_app(service, daily=daily, exports=exports))
    ) as client:
        with monkeypatch.context() as patch:
            # web.json_response binds its default serializer at import time.
            import camera_media_service.daily_http_api as routes

            real_response = routes.web.json_response

            def response(data=None, **kwargs):
                return real_response(data, dumps=dumps, **kwargs)

            patch.setattr(routes.web, "json_response", response)
            response = await client.get("/api/v2/daily-recordings/" + old["recording_id"])
            assert response.status == 200, await response.text()


@pytest.mark.asyncio
async def test_daily_segment_snapshot_decoding_leaves_event_loop(tmp_path, monkeypatch):
    import json

    from test_daily_export import recorded_day

    service, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    real = json.loads
    main = threading.get_ident()

    def loads(body, *args, **kwargs):
        if isinstance(body, str) and '"segment_id"' in body:
            assert threading.get_ident() != main, "daily segment JSON decoded on shared event loop"
        return real(body, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(json, "loads", loads)
            _, segments = await daily.playback_snapshot(day["recording_id"])
            assert segments
    finally:
        daily.initialized = False
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_media_open_closes_worker_result(tmp_path, monkeypatch):
    path = tmp_path / "open.mp4"
    path.write_bytes(b"media")
    entered, release = threading.Event(), threading.Event()
    real_open = Path.open
    handles = []

    def opened(self, *args, **kwargs):
        file = real_open(self, *args, **kwargs)
        handles.append(file)
        entered.set()
        assert release.wait(2)
        return file

    monkeypatch.setattr(Path, "open", opened)
    task = asyncio.create_task(media_files.open_media_file(path))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles and handles[0].closed


@pytest.mark.asyncio
async def test_stream_rechecks_storage_during_download(tmp_path):
    from aiohttp import ClientPayloadError, web
    from aiohttp.test_utils import TestClient, TestServer

    path = tmp_path / "stream.mp4"
    path.write_bytes(b"m" * (65536 * 2))
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("Media mount disappeared")

    async def content(request):
        file, size = await media_files.open_media_file(path)
        return await media_files.stream_file(
            request,
            file,
            size=size,
            filename="media.mp4",
            content_type="video/mp4",
            guard=guard,
        )

    app = web.Application()
    app.router.add_get("/media", content)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/media")
        assert response.status == 200
        with pytest.raises(ClientPayloadError):
            await response.read()
    assert calls == 2


@pytest.mark.asyncio
async def test_archive_cleanup_refuses_unavailable_media_mount(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch, ttl_seconds=1)
    try:
        file, _ = await playback.open_segment(day["recording_id"], parts[0]["segment_id"])
        path = Path(file.name)
        file.close()

        def unavailable():
            raise OSError("Media mount unavailable")

        service.storage_guard = unavailable
        with pytest.raises(OSError, match="mount unavailable"):
            await playback.maintenance(clock[0] + 2)
        assert path.exists()
    finally:
        service.storage_guard = lambda: None
        await shutdown(service, daily, playback)
