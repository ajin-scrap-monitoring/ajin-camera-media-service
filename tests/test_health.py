import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_recording import make_service

from camera_media_service.http_api import create_http_app


@pytest.mark.asyncio
async def test_health_reads_catalogue_without_waiting_for_writer_lock(tmp_path):
    service = make_service(tmp_path)
    async with TestClient(TestServer(create_http_app(service))) as client:
        async with service.lock:
            async with service.transaction():
                service.db.execute("INSERT INTO pending_cleanup VALUES('health-fixture')")
                response = await asyncio.wait_for(client.get("/healthz"), 0.5)
                assert response.status == 200, await response.text()
                assert await response.json() == {"status": "ok"}
                assert service.db.in_transaction
        assert service.db.execute(
            "SELECT 1 FROM pending_cleanup WHERE id='health-fixture'"
        ).fetchone()


@pytest.mark.asyncio
async def test_health_does_not_hide_missing_storage(tmp_path):
    service = make_service(tmp_path)
    async with TestClient(TestServer(create_http_app(service))) as client:

        def denied():
            raise OSError("Expected storage unavailable")

        service.storage_guard = denied
        response = await client.get("/healthz")
        assert response.status == 503
