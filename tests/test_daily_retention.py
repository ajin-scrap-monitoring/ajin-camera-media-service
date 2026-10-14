from datetime import date

import pytest
from test_daily_export import make_exports, recorded_day, wait_ready
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.recording import ApiError, epoch


@pytest.mark.asyncio
async def test_export_cache_expires_before_original_and_can_be_prepared_again(
    tmp_path, monkeypatch
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        await exports.request_export(old["recording_id"])
        ready = await wait_ready(exports, daily, old["recording_id"])
        clock[0] = epoch(ready["export_expires_at"]) + 1
        await exports.maintenance(clock[0])
        day = await daily.get_day(old["recording_id"])
        assert day["status"] == "closed" and day["export_state"] == "not_requested"
        assert daily.catalog.segments(old["recording_id"], ready_only=True)
        _, created = await exports.request_export(old["recording_id"])
        assert created
        assert (await wait_ready(exports, daily, old["recording_id"]))["export_state"] == "ready"
        clock[0] = epoch(old["expires_at"]) + 1
        await exports.maintenance(clock[0])
        expired = await daily.get_day(old["recording_id"])
        assert expired["status"] == "expired" and expired["download_path"] is None
        assert not list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
        with pytest.raises(ApiError) as error:
            await exports.request_export(old["recording_id"])
        assert error.value.status == 410
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_closed_empty_day_is_not_exportable(tmp_path, monkeypatch):
    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at - 10]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    exports = make_exports(daily)
    try:
        await daily.tick(clock[0])
        clock[0] = window.end_at
        await daily.tick(clock[0])
        old = next(
            r
            for r in await daily.list_days("camera-main", {}, 20, None)
            if r["recording_date"] == "2026-10-14"
        )
        assert old["status"] == "closed" and old["coverage"] == "none"
        with pytest.raises(ApiError) as error:
            await exports.request_export(old["recording_id"])
        assert error.value.code == "NO_RECORDING_DATA"
    finally:
        await exports.close()
        await daily.close()
        await service.close()
