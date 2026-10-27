import asyncio
import importlib.util
import subprocess
import uuid
from datetime import date
from pathlib import Path

import imageio_ffmpeg
import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow
from camera_media_service.encoding import merge_daily_mp4, probe_media
from camera_media_service.recording import ApiError, iso


async def seeded(tmp_path, monkeypatch, *, frame_counts=(30, 30), **options):
    assert importlib.util.find_spec("camera_media_service.archive_playback"), (
        "Archive playback missing"
    )
    from camera_media_service.archive_playback import ArchivedPlaybackService

    window = DailyWindow.from_date(date(2026, 10, 14))
    clock = [window.end_at + 10]
    service, daily = make_daily(tmp_path, clock, monkeypatch)
    service.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    day = daily.catalog.ensure_day("camera-main", window, 30)
    day.update(
        status="closed",
        archive_state="mp4",
        export_state="ready",
        recorded_seconds=2,
        expires_at=iso(window.end_at + 86400),
    )
    folder = tmp_path / "daily" / day["recording_id"]
    folder.mkdir(parents=True)
    parts = []
    for index, color in enumerate(("blue", "red")):
        path = folder / f"{uuid.uuid4()}.ts"
        subprocess.run(
            [
                service.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x240:r=30",
                "-frames:v",
                str(frame_counts[index]),
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-g",
                "30",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "mpegts",
                str(path),
            ],
            check=True,
        )
        segment = daily.catalog.begin_segment(
            day["recording_id"], window.start_at + index, window.start_at + index + 1
        )
        segment.update(
            status="ready",
            path=path.relative_to(tmp_path).as_posix(),
            duration=frame_counts[index] / 30,
            size_bytes=path.stat().st_size,
            archive_path=f"daily-exports/{day['recording_id']}.mp4",
            archive_offset=sum(frame_counts[:index]) / 30,
            profile={"width": 320, "height": 240, "fps": 30},
        )
        daily.catalog.complete_segment(segment["segment_id"], segment)
        parts.append(segment)
    final = tmp_path / "daily-exports" / f"{day['recording_id']}.mp4"
    await merge_daily_mp4(service.ffmpeg, parts, final.with_suffix(".tmp.mp4"), final)
    for segment in parts:
        (tmp_path / segment["path"]).unlink()
    daily.catalog.save_day(day)
    daily.db.commit()
    playback = ArchivedPlaybackService(daily, **options)
    return service, daily, playback, day, parts, clock


async def shutdown(service, daily, playback):
    await playback.close()
    await daily.close()
    await service.close()


@pytest.mark.asyncio
async def test_archive_slice_fully_decodes_with_correct_duration_and_initial_frame(
    tmp_path, monkeypatch
):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch)
    try:
        file, size = await playback.open_segment(day["recording_id"], parts[1]["segment_id"])
        path = file.name
        assert size == len(file.read()) and size > 0
        file.close()
        metadata = await probe_media(service.ffmpeg, Path(path))
        assert metadata["duration"] == pytest.approx(1.0, abs=0.04)
        reader = imageio_ffmpeg.read_frames(
            str(path), pix_fmt="rgb24", output_params=["-vsync", "0"]
        )
        next(reader)
        first = next(reader)
        reader.close()
        assert first[0] > 220 and first[2] < 30
        assert daily.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 0
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_same_slice_coalesces_and_cache_expires(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch, ttl_seconds=5)
    try:
        results = await asyncio.gather(
            *[playback.open_segment(day["recording_id"], parts[0]["segment_id"]) for _ in range(8)]
        )
        names = {f.name for f, size in results}
        assert len(names) == 1
        for file, _size in results:
            file.close()
        paths = list((tmp_path / "daily-playback").rglob("*.ts"))
        assert len(paths) == 1
        await playback.maintenance(clock[0] + 6)
        assert not list((tmp_path / "daily-playback").rglob("*.ts"))
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_cancel_queued_job_releases_lease_and_capacity(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(
        tmp_path, monkeypatch, concurrency=1, max_jobs=1
    )
    await playback.slot.acquire()
    task = asyncio.create_task(playback.open_segment(day["recording_id"], parts[0]["segment_id"]))
    try:
        for _ in range(200):
            if daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone():
                break
            await asyncio.sleep(0.005)
        assert (
            daily.db.execute("SELECT clip_id FROM clip_recording_pins")
            .fetchone()[0]
            .startswith("hls:")
        )
        with pytest.raises(ApiError) as error:
            await playback.open_segment(day["recording_id"], parts[1]["segment_id"])
        assert error.value.status == 503
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not list((tmp_path / "daily-playback").rglob("*.tmp"))
    finally:
        playback.slot.release()
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_cache_byte_cap_evicts_and_rejects_oversized_output(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(
        tmp_path, monkeypatch, max_bytes=17000
    )
    try:
        for segment in parts:
            file, size = await playback.open_segment(day["recording_id"], segment["segment_id"])
            file.close()
            assert (
                sum(
                    p.stat().st_size
                    for p in (tmp_path / "daily-playback").rglob("*")
                    if p.is_file()
                )
                <= 17000
            )
        assert len(list((tmp_path / "daily-playback").rglob("*.ts"))) == 1
        playback.max_bytes = 100
        with pytest.raises(ApiError):
            await playback.open_segment(day["recording_id"], parts[0]["segment_id"])
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["bad_day", "bad_segment", "missing", "deleted", "expired", "escape"]
)
async def test_unavailable_or_malformed_archives_are_rejected(tmp_path, monkeypatch, mutation):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch)
    day_id, segment_id = day["recording_id"], parts[0]["segment_id"]
    try:
        expected = 404
        if mutation == "bad_day":
            day_id, expected = "../outside", 400
        elif mutation == "bad_segment":
            segment_id, expected = "../outside", 400
        elif mutation == "missing":
            segment_id = str(uuid.uuid4())
        elif mutation in ("deleted", "expired"):
            day["status"] = mutation
            daily.catalog.save_day(day)
            expected = 410
        else:
            parts[0]["archive_path"] = "../outside.mp4"
            daily.catalog.complete_segment(segment_id, parts[0])
            expected = 503
        with pytest.raises(ApiError) as error:
            await playback.open_segment(day_id, segment_id)
        assert error.value.status == expected
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", [(31, 17), (29, 31), (1, 29)])
async def test_fractional_cfr_boundaries_preserve_exact_frame_count(tmp_path, monkeypatch, counts):
    service, daily, playback, day, parts, clock = await seeded(
        tmp_path, monkeypatch, frame_counts=counts
    )
    try:
        for index, item in enumerate(parts):
            file, size = await playback.open_segment(day["recording_id"], item["segment_id"])
            path = file.name
            file.close()
            reader = imageio_ffmpeg.read_frames(
                str(path), pix_fmt="rgb24", output_params=["-vsync", "0"]
            )
            next(reader)
            frames = list(reader)
            assert len(frames) == counts[index]
            for frame in frames:
                assert frame[0 if index else 2] > 220
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_cancel_running_ffmpeg_terminates_process_and_releases_pin(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch)
    process_started = asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        # A real FFmpeg process stays alive awaiting stdin, letting cancellation
        # exercise actual process termination, pipes and source pin cleanup.
        process = await original(
            service.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-f",
            "mpegts",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            **kwargs,
        )
        processes.append(process)
        process_started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(playback.open_segment(day["recording_id"], parts[0]["segment_id"]))
    try:
        await asyncio.wait_for(process_started.wait(), 5)
        assert daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not daily.storage_reservations
        assert not list((tmp_path / "daily-playback").rglob("*.tmp"))
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_startup_locked_stale_cache_still_counts_toward_cap(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch)
    from camera_media_service.archive_playback import ArchivedPlaybackService

    await playback.close()
    folder = tmp_path / "daily-playback" / day["recording_id"]
    folder.mkdir(exist_ok=True)
    stale = folder / f"{uuid.uuid4()}.ts"
    stale.write_bytes(b"x" * 10000)
    # Windows refuses to unlink an open response handle. The bytes must keep
    # consuming the cap, including after service restart cleanup cannot unlink.
    held = stale.open("rb")
    unlink = Path.unlink

    def locked_unlink(path, *args, **kwargs):
        # Exercise that failure on POSIX too, where real open handles may be unlinked.
        if path == stale and not held.closed:
            raise PermissionError("Simulated open response handle")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    playback = ArchivedPlaybackService(daily, max_bytes=17000)
    try:
        with pytest.raises(ApiError) as error:
            await playback.open_segment(day["recording_id"], parts[0]["segment_id"])
        assert error.value.status == 503
        assert (
            sum(p.stat().st_size for p in (tmp_path / "daily-playback").rglob("*") if p.is_file())
            <= 17000
        )
    finally:
        held.close()
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_oversized_running_ffmpeg_is_reaped_and_leaves_no_partial_cache(
    tmp_path, monkeypatch
):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch, max_bytes=100)
    processes = []
    original = asyncio.create_subprocess_exec

    async def noisy(*args, **kwargs):
        process = await original(
            service.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x480:r=30",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-t",
            "600",
            "-f",
            "mpegts",
            "pipe:1",
            **kwargs,
        )
        original_read = process.stdout.read

        async def delayed_read(size):
            data = await original_read(size)
            await asyncio.sleep(0.2)
            return data

        process.stdout.read = delayed_read
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", noisy)
    try:
        with pytest.raises(ApiError) as error:
            await asyncio.wait_for(
                playback.open_segment(day["recording_id"], parts[0]["segment_id"]), 5
            )
        assert error.value.status == 503
        assert processes[0].returncode is not None
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
        assert not list((tmp_path / "daily-playback").rglob("*.tmp"))
    finally:
        await shutdown(service, daily, playback)


@pytest.mark.asyncio
async def test_cancel_while_subprocess_creation_is_returning_reaps_process(tmp_path, monkeypatch):
    service, daily, playback, day, parts, clock = await seeded(tmp_path, monkeypatch)
    process_started, release = asyncio.Event(), asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        process = await original(
            service.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "image2pipe",
            "-i",
            "pipe:0",
            "-f",
            "mpegts",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            **kwargs,
        )
        processes.append(process)
        process_started.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(playback.open_segment(day["recording_id"], parts[0]["segment_id"]))
    try:
        await asyncio.wait_for(process_started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
        assert not daily.db.execute("SELECT 1 FROM clip_recording_pins").fetchone()
    finally:
        release.set()
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        await shutdown(service, daily, playback)
