import asyncio

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer


@pytest.mark.asyncio
async def test_probe_retries_live_when_camera_has_not_sent_first_frame():
    from tools.daily_server_probe import consume_mjpeg

    attempts = 0
    received = asyncio.Event()

    async def live(request):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return web.Response(status=503)
        return web.Response(body=b"synthetic-jpeg")

    app = web.Application()
    app.router.add_get("/live", live)
    async with TestServer(app) as server, ClientSession() as client:
        task = asyncio.create_task(
            consume_mjpeg(client, str(server.make_url("/live")), lambda n: received.set())
        )
        try:
            await asyncio.wait_for(received.wait(), timeout=2)
            assert attempts >= 3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
