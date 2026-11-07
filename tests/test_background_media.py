import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

from camera_media_service.recording_diagnostics import RecordingDiagnostics


def owner():
    return SimpleNamespace(
        queue=asyncio.Queue(1200),
        daily_frame_watermarks={"cam": time.time()},
        storage_error=False,
        diagnostics=RecordingDiagnostics(),
    )


@pytest.mark.asyncio
async def test_verified_duplicate_reclamation_can_recover_storage_error(tmp_path, monkeypatch):
    from test_daily_archive import archive_ready
    from test_daily_export import make_exports, recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    sources = list((recording.root / "daily" / day["recording_id"]).rglob("*.ts"))
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, day["recording_id"]))["archive_state"] == "mp4"
        recording.queue = asyncio.Queue(1200)
        recording.storage_error = True
        await exports.maintenance(clock[0])
        assert sources and not any(path.exists() for path in sources)
    finally:
        recording.queue = None
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_background_admission_waits_without_taking_recording_lock():
    from camera_media_service.background_media import BackgroundMedia

    recording = owner()
    for _ in range(80):
        recording.queue.put_nowait(None)
    control = BackgroundMedia(recording)
    task = asyncio.create_task(control.checkpoint())
    await asyncio.sleep(0.03)
    assert not task.done()
    while not recording.queue.empty():
        recording.queue.get_nowait()
    await asyncio.wait_for(task, 1)
    assert recording.diagnostics.loss_counts["background_deferred"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Owned-child SIGSTOP/SIGCONT are POSIX controls")
async def test_running_owned_process_pauses_and_resumes_and_is_reaped(tmp_path):
    from camera_media_service.background_media import BackgroundMedia
    from camera_media_service.encoding import start_media_process

    recording = owner()
    control = BackgroundMedia(recording)
    path = tmp_path / "ticks"
    code = (
        "import pathlib,time; p=pathlib.Path(__import__('sys').argv[1]);\n"
        "while True:\n with p.open('ab') as f:f.write(b'x')\n time.sleep(.005)"
    )
    process = None
    try:
        async with control.scope():
            process = await start_media_process(sys.executable, "-c", code, str(path))
            for _ in range(100):
                if path.exists():
                    break
                await asyncio.sleep(0.01)
            assert path.exists()
            for _ in range(80):
                recording.queue.put_nowait(None)
            for _ in range(100):
                if recording.diagnostics.gauges.get("background_process_paused") == 1:
                    break
                await asyncio.sleep(0.01)
            assert recording.diagnostics.gauges.get("background_process_paused") == 1
            await asyncio.sleep(0.05)
            size = path.stat().st_size
            await asyncio.sleep(0.1)
            assert path.stat().st_size == size
            while not recording.queue.empty():
                recording.queue.get_nowait()
            for _ in range(200):
                if path.stat().st_size > size:
                    break
                await asyncio.sleep(0.01)
            assert path.stat().st_size > size
            for _ in range(80):
                recording.queue.put_nowait(None)
            await asyncio.sleep(0.15)
            process.kill()
            await asyncio.wait_for(process.wait(), 1)
        assert not control.watchers
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_archive_does_not_reserve_or_start_when_live_queue_is_pressured(
    tmp_path, monkeypatch
):
    from test_daily_export import make_exports, recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    recording.queue = asyncio.Queue(1200)
    for _ in range(80):
        recording.queue.put_nowait(None)
    exports = make_exports(daily)
    try:
        await exports.maintenance(clock[0])
        assert not exports.jobs
        assert not exports.reservations
        assert (await daily.get_day(day["recording_id"]))["export_state"] == "not_requested"
    finally:
        recording.queue = None
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_compaction_stops_between_batches_when_pressure_arrives(tmp_path, monkeypatch):
    from pathlib import Path

    from test_daily_archive import archive_ready
    from test_daily_export import make_exports, recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    folder = recording.root / "daily" / day["recording_id"]
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, day["recording_id"]))["archive_state"] == "mp4"
        for i in range(150):
            (folder / f"owned-fixture-{i}.ts").write_bytes(b"owned test file")
        recording.queue = asyncio.Queue(1200)
        original = Path.unlink
        loop = asyncio.get_running_loop()
        removed = []

        def pressure():
            for _ in range(80):
                recording.queue.put_nowait(None)

        def unlink(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.is_relative_to(folder):
                removed.append(path)
                if len(removed) == 64:
                    loop.call_soon_threadsafe(pressure)
            return result

        monkeypatch.setattr(Path, "unlink", unlink)
        await exports.maintenance(clock[0])
        assert len(removed) == 64 and folder.exists()
        assert recording.db.execute(
            "SELECT 1 FROM daily_compactions WHERE day_id=?", (day["recording_id"],)
        ).fetchone()
        recording.queue = None
        await exports.maintenance(clock[0])
        assert not folder.exists()
    finally:
        recording.queue = None
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_stream_copy_uses_file_input_pacing(tmp_path, monkeypatch):
    from test_daily_export import recorded_day

    import camera_media_service.encoding as module
    from camera_media_service.background_media import BackgroundMedia

    recording, daily, _, day = await recorded_day(tmp_path, monkeypatch)
    original = module.start_media_process
    observed = []

    async def checked(*args, **kwargs):
        if "copy" in args:
            observed.append(args)
            assert "-readrate" in args
            assert args[args.index("-readrate") + 1] == "8"
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "start_media_process", checked)
    final = recording.root / "daily-exports" / "paced.mp4"
    try:
        async with BackgroundMedia(recording, clock=daily.clock).scope():
            metadata = await module.merge_daily_mp4(
                recording.ffmpeg,
                daily.catalog.segments(day["recording_id"], ready_only=True),
                final.with_suffix(".tmp.mp4"),
                final,
            )
        assert observed and metadata["duration"] > 0
    finally:
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_source_compaction_defers_while_live_queue_is_pressured(tmp_path, monkeypatch):
    from test_daily_archive import archive_ready
    from test_daily_export import make_exports, recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    originals = list((recording.root / "daily" / day["recording_id"]).rglob("*.ts"))
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, day["recording_id"]))["archive_state"] == "mp4"
        recording.queue = asyncio.Queue(1200)
        for _ in range(80):
            recording.queue.put_nowait(None)
        await exports.maintenance(clock[0])
        assert originals and all(path.exists() for path in originals)
        assert recording.db.execute(
            "SELECT 1 FROM daily_compactions WHERE day_id=?", (day["recording_id"],)
        ).fetchone()
        recording.queue = None
        await exports.maintenance(clock[0])
        assert not any(path.exists() for path in originals)
    finally:
        recording.queue = None
        await exports.close()
        await daily.close()
        await recording.close()
