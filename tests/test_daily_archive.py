import asyncio

import pytest
from test_daily_export import make_exports, recorded_day


async def archive_ready(exports, daily, day_id):
    for _ in range(500):
        day = await daily.get_day(day_id)
        if day["export_state"] in ("ready", "failed"):
            return day
        await asyncio.sleep(0.01)
    pytest.fail("Automatic archival did not complete")


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "truncated"])
async def test_damaged_master_before_compaction_recovers_from_retained_sources(
    tmp_path, monkeypatch, damage
):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    key = old["recording_id"]
    sources = list((service.root / "daily" / key).rglob("*.ts"))
    with service.db:
        service.db.execute("INSERT INTO clip_recording_pins VALUES(?,?)", ("hold", key))
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["export_state"] == "ready"
        path = exports._path(key)
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"damaged")
        with service.db:
            service.db.execute("DELETE FROM clip_recording_pins WHERE clip_id='hold'")
        await exports.maintenance(clock[0])
        assert sources and all(p.exists() for p in sources)
        day = await daily.get_day(key)
        assert day["archive_state"] == "segments"
        assert day["export_state"] == "failed"
        clock[0] += 31
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["export_state"] == "ready"
        await exports.maintenance(clock[0])
        assert not any(p.exists() for p in sources)
        assert path.stat().st_size > 7
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_download_prepare_does_not_start_whole_day_work(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        _, created = await exports.request_export(old["recording_id"])
        assert not created
        assert not exports.jobs
        assert not list(exports.output.glob("*.mp4"))
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_closed_day_is_automatically_archived_and_sources_compacted(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    originals = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    assert originals
    try:
        await exports.maintenance(clock[0])
        assert old["recording_id"] in exports.jobs
        day = await archive_ready(exports, daily, old["recording_id"])
        assert day["export_state"] == "ready", day
        assert day["archive_state"] == "mp4"
        assert day["export_expires_at"] == old["expires_at"]
        path = exports.output / (old["recording_id"] + ".mp4")
        assert path.is_file()
        await exports.maintenance(clock[0])
        assert all(not p.exists() for p in originals)
        items = daily.catalog.segments(old["recording_id"], ready_only=True)
        assert items[0]["archive_path"] == str(path.relative_to(service.root))
        assert items[0]["archive_offset"] == 0
        assert items[0]["start_at"] < items[0]["end_at"]
        modified = path.stat().st_mtime_ns
        clock[0] += 25 * 3600
        await exports.maintenance(clock[0])
        assert path.is_file() and path.stat().st_mtime_ns == modified
        assert (await exports.request_export(old["recording_id"]))[0]["export_state"] == "ready"
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_failed_archive_keeps_sources_and_background_retries(tmp_path, monkeypatch):
    import camera_media_service.daily_export as module

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    real = module.merge_daily_mp4
    originals = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))

    async def fail(*args):
        raise OSError("Disk write failed")

    monkeypatch.setattr(module, "merge_daily_mp4", fail)
    try:
        await exports.maintenance(clock[0])
        assert old["recording_id"] in exports.jobs
        failed = await archive_ready(exports, daily, old["recording_id"])
        assert failed["export_state"] == "failed"
        assert all(p.exists() for p in originals)
        assert failed.get("archive_state") != "mp4"
        monkeypatch.setattr(module, "merge_daily_mp4", real)
        clock[0] += 31
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["archive_state"] == "mp4"
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_source_cleanup_waits_for_active_clip_lease(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    originals = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    with service.db:
        service.db.execute(
            "INSERT INTO clip_recording_pins VALUES(?,?)", ("existing-clip", old["recording_id"])
        )
    try:
        await exports.maintenance(clock[0])
        assert old["recording_id"] in exports.jobs
        assert (await archive_ready(exports, daily, old["recording_id"]))["archive_state"] == "mp4"
        await exports.maintenance(clock[0])
        assert all(p.exists() for p in originals)
        with service.db:
            service.db.execute("DELETE FROM clip_recording_pins")
        await exports.maintenance(clock[0])
        assert all(not p.exists() for p in originals)
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_no_temporary_headroom_keeps_originals(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    originals = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    daily.settings.max_daily_storage_bytes = sum(p.stat().st_size for p in originals) + 512
    try:
        await exports.maintenance(clock[0])
        day = await daily.get_day(old["recording_id"])
        assert day["export_state"] == "failed"
        assert day["error"]["code"] == "INSUFFICIENT_STORAGE"
        assert all(p.exists() for p in originals)
        assert not exports.jobs
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_archive_flush_failure_never_discards_sources(tmp_path, monkeypatch):
    import camera_media_service.encoding as encoding

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    originals = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))

    def fail_flush(fd):
        raise OSError("Storage flush failed")

    monkeypatch.setattr(encoding.os, "fsync", fail_flush)
    try:
        await exports.maintenance(clock[0])
        result = await archive_ready(exports, daily, old["recording_id"])
        assert result["export_state"] == "failed"
        assert result.get("archive_state") != "mp4"
        assert all(p.exists() for p in originals)
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_staged_archive_switch_preserves_originals_before_activation(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    daily.settings.daily_archive_enabled = False
    exports = make_exports(daily)
    originals = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    try:
        await exports.maintenance(clock[0])
        assert not exports.jobs
        assert all(p.exists() for p in originals)
        daily.settings.daily_archive_enabled = True
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["archive_state"] == "mp4"
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_archive_disk_space_scan_does_not_block_ingest_catalog(tmp_path, monkeypatch):
    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    entered, release = asyncio.Event(), asyncio.Event()
    real = daily.check_space

    async def slow(reserve=0):
        entered.set()
        await release.wait()
        await real(reserve)

    monkeypatch.setattr(daily, "check_space", slow)
    task = asyncio.create_task(exports.maintenance(clock[0]))
    try:
        await entered.wait()
        policy = await asyncio.wait_for(service.get_policy("camera-main"), timeout=0.3)
        assert policy["pre_roll_seconds"] == 30
    finally:
        release.set()
        await task
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_restart_finishes_committed_source_compaction_without_rebuilding_mp4(
    tmp_path, monkeypatch
):
    from test_daily_recording import make_daily

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    original = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    with service.db:
        service.db.execute(
            "INSERT INTO clip_recording_pins VALUES(?,?)", ("lease", old["recording_id"])
        )
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["archive_state"] == "mp4"
        master = exports.output / (old["recording_id"] + ".mp4")
        modified = master.stat().st_mtime_ns
        assert all(p.exists() for p in original)
        with service.db:
            service.db.execute("DELETE FROM clip_recording_pins")
        await exports.close()
        await daily.close()
        await service.close()
        service, daily = make_daily(tmp_path, clock, monkeypatch)
        exports = make_exports(daily)
        await exports.maintenance(clock[0])
        assert not exports.jobs
        assert master.exists() and master.stat().st_mtime_ns == modified
        assert all(not p.exists() for p in original)
        assert (await exports.request_export(old["recording_id"]))[0]["export_state"] == "ready"
    finally:
        await exports.close()
        await daily.close()
        await service.close()


@pytest.mark.asyncio
async def test_interrupted_verification_keeps_ts_and_recovers_automatically(tmp_path, monkeypatch):
    import camera_media_service.daily_export as module

    service, daily, clock, old = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    original = list((service.root / "daily" / old["recording_id"]).rglob("*.ts"))
    entered = asyncio.Event()
    verify = module.verify_archive

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "verify_archive", blocked)
    try:
        await exports.maintenance(clock[0])
        await entered.wait()
        await exports.close()
        assert all(p.exists() for p in original)
        assert (await daily.get_day(old["recording_id"]))["archive_state"] == "segments"
        monkeypatch.setattr(module, "verify_archive", verify)
        exports = make_exports(daily)
        clock[0] += 31
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, old["recording_id"]))["archive_state"] == "mp4"
    finally:
        await exports.close()
        await daily.close()
        await service.close()
