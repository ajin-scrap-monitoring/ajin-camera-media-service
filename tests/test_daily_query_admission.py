import asyncio
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_daily_export import make_exports, recorded_day

from camera_media_service.daily_queries import DailySnapshots
from camera_media_service.http_api import create_http_app


@pytest.mark.asyncio
async def test_slow_snapshot_queries_bound_admission_without_blocking_event_loop(
    tmp_path, monkeypatch
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    entered, release = threading.Event(), threading.Event()
    original = DailySnapshots.page

    def slow(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DailySnapshots, "page", slow)
    async with TestClient(
        TestServer(create_http_app(service, daily=daily, exports=exports))
    ) as client:
        path = "/api/v2/daily-recordings/" + old["recording_id"] + "/gaps"
        requests = [asyncio.create_task(client.get(path))]
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            requests.extend(asyncio.create_task(client.get(path)) for _ in range(3))
            await asyncio.sleep(0.1)
            rejected = await asyncio.wait_for(client.get(path), 1)
            assert rejected.status == 503
            assert (await rejected.json())["error"]["code"] == "SERVICE_BUSY"
            assert not requests[0].done()
            release.set()
            responses = await asyncio.gather(*requests)
            assert all(response.status == 200 for response in responses)
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
