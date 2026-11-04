import asyncio
import shutil
import time

import imageio_ffmpeg
import pytest
from test_media_info import make_video
from test_recording import make_service

from camera_media_service.recording import ApiError, iso


async def ready_clip(tmp_path):
    recording = make_service(tmp_path)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    body, _ = await recording.create_clip("camera-main", "thumbnail-fixture")
    video = make_video(tmp_path, width=320, height=240)
    shutil.copyfile(video, recording.clips / (body["clip_id"] + ".mp4"))
    body.update(
        status="ready",
        completed_at=iso(time.time()),
        expires_at=iso(time.time() + 86400),
        actual_start_at=iso(1000),
        actual_end_at=iso(1000.5),
        available_intervals=[
            {
                "start_at": iso(1000),
                "end_at": iso(1000.5),
                "playback_start_seconds": 0,
                "playback_end_seconds": 0.5,
            }
        ],
    )
    with recording.db:
        recording._save(body)
    return recording, body


async def finished(service, kind, key):
    for _ in range(200):
        info = await service.info(kind, key)
        if info["status"] != "pending":
            return info
        await asyncio.sleep(0.02)
    pytest.fail("Thumbnail did not finish")


@pytest.mark.asyncio
async def test_explicit_prepare_actual_jpeg_persists_and_get_never_generates(tmp_path, monkeypatch):
    from PIL import Image

    from camera_media_service.thumbnails import ThumbnailService

    recording, body = await ready_clip(tmp_path)
    service = ThumbnailService(recording)
    await service.start()
    key = body["clip_id"]
    try:
        info = await service.info("clip", key)
        assert info["status"] == "unavailable" and info["reason_code"] == "NOT_PREPARED"
        assert not service.jobs
        response, created = await service.prepare("clip", key)
        assert created and response["status"] == "pending"
        info = await finished(service, "clip", key)
        assert info["status"] == "ready", info
        assert info["representative_at"] is None
        assert info["time_basis"] is None
        file, size = await service.open_image("clip", key)
        try:
            with Image.open(file) as image:
                assert image.size == (320, 240)
        finally:
            file.close()
        assert info["size_bytes"] == size and info["camera_id"] == "camera-main"
        assert not any(str(recording.root) in str(v) for v in info.values())
        await service.close()
        service = ThumbnailService(recording)
        await service.start()
        service.ffmpeg = "missing-executable"
        assert await service.info("clip", key) == info
        response, created = await service.prepare("clip", key)
        assert response == info and not created
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_same_size_mtime_replacement_and_parent_delete_never_serve_stale(tmp_path):
    from camera_media_service.thumbnails import ThumbnailService

    recording, body = await ready_clip(tmp_path)
    service = ThumbnailService(recording)
    await service.start()
    key = body["clip_id"]
    try:
        await service.prepare("clip", key)
        assert (await finished(service, "clip", key))["status"] == "ready"
        path = recording.clips / (key + ".mp4")
        old = path.stat()
        other = path.with_suffix(".replacement")
        shutil.copyfile(path, other)
        import os

        os.utime(other, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(other, path)
        info = await service.info("clip", key)
        assert info["status"] == "unavailable" and info["content_path"] is None
        await recording.delete_clip(key)
        for operation in (service.info, service.prepare, service.open_image):
            with pytest.raises(ApiError) as error:
                await operation("clip", key)
            assert error.value.status == 410
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_generate_preserves_portrait_aspect_and_does_not_upscale(tmp_path):
    from PIL import Image

    from camera_media_service.thumbnail_media import generate_thumbnail

    for width, height, expected in [(1080, 1920, (202, 360)), (160, 120, (160, 120))]:
        folder = tmp_path / str(width)
        folder.mkdir()
        source = make_video(folder, width=width, height=height)
        output = folder / "thumbnail.jpg"
        metadata = await generate_thumbnail(imageio_ffmpeg.get_ffmpeg_exe(), source, output)
        with Image.open(output) as image:
            assert image.size[1] == expected[1]
            assert abs(image.size[0] - expected[0]) <= 1
        assert metadata["size_bytes"] == output.stat().st_size
        assert output.read_bytes()[:2] == bytes([255, 216])


@pytest.mark.asyncio
async def test_new_generation_attestation_preserves_first_real_frame_time(tmp_path):
    from camera_media_service.persisted_media_info import file_identity
    from camera_media_service.thumbnails import ThumbnailService

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = ThumbnailService(recording)
    await service.start()
    try:
        recording.notify_thumbnail_ready(
            "clip",
            key,
            {"_file_identity": file_identity((recording.clips / (key + ".mp4")).stat())},
        )
        for _ in range(200):
            info = await service.info("clip", key)
            if info["status"] == "ready":
                break
            await asyncio.sleep(0.02)
        assert info["status"] == "ready" and info["representative_at"] == iso(1000)
        assert info["time_basis"] == "server_received_at"
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_generation_failure_is_optional_and_retry_is_explicit(tmp_path, monkeypatch):
    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = thumbnails.ThumbnailService(recording)
    await service.start()
    original = thumbnails.generate_thumbnail

    async def timeout(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(thumbnails, "generate_thumbnail", timeout)
    try:
        await service.prepare("clip", key)
        info = await finished(service, "clip", key)
        assert info["status"] == "failed" and info["reason_code"] == "GENERATION_TIMEOUT"
        assert (await recording.get_clip(key))["status"] == "ready"
        assert not (await service.prepare("clip", key))[1]
        monkeypatch.setattr(thumbnails, "generate_thumbnail", original)
        assert (await service.prepare("clip", key, retry_failed=True))[1]
        assert (await finished(service, "clip", key))["status"] == "ready"
        assert not recording.capacity_reservations and not recording.thumbnail_pins
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_singleflight_capacity_and_delete_drains_source_reader(tmp_path, monkeypatch):
    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = thumbnails.ThumbnailService(recording, max_jobs=1)
    await service.start()
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            draining.set()
            await release.wait()

    monkeypatch.setattr(thumbnails, "generate_thumbnail", blocked)
    deletion = None
    try:
        await service.prepare("clip", key)
        await entered.wait()
        assert key in recording.thumbnail_pins
        responses = await asyncio.gather(*[service.prepare("clip", key) for _ in range(8)])
        assert not any(created for _, created in responses) and len(service.jobs) == 1
        second, _ = await recording.create_clip("camera-main", "capacity")
        other = second["clip_id"]
        shutil.copyfile(recording.clips / (key + ".mp4"), recording.clips / (other + ".mp4"))
        second.update(status="ready", expires_at=iso(time.time() + 10000))
        with recording.db:
            recording._save(second)
        with pytest.raises(ApiError) as error:
            await service.prepare("clip", other)
        assert error.value.code == "THUMBNAIL_CAPACITY_EXCEEDED"
        deletion = asyncio.create_task(recording.delete_clip(key))
        await asyncio.wait_for(draining.wait(), 1)
        assert not deletion.done() and (recording.clips / (key + ".mp4")).exists()
        release.set()
        await deletion
        assert not recording.thumbnail_pins and not recording.capacity_reservations
        with pytest.raises(ApiError) as error:
            await service.info("clip", key)
        assert error.value.status == 410
    finally:
        release.set()
        if deletion:
            await asyncio.gather(deletion, return_exceptions=True)
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_daily_requires_existing_mp4_and_delete_cleans_flat_image(tmp_path, monkeypatch):
    from test_daily_export import make_exports, recorded_day, wait_ready

    from camera_media_service.thumbnails import ThumbnailService

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    service = ThumbnailService(recording, daily, exports)
    await service.start()
    key = day["recording_id"]
    try:
        info, created = await service.prepare("day", key)
        assert not created and info["reason_code"] == "DAILY_MP4_REQUIRED" and not exports.jobs
        await exports.maintenance(clock[0])
        await wait_ready(exports, daily, key)
        for _ in range(200):
            info = await service.info("day", key)
            if info["status"] == "ready":
                break
            await asyncio.sleep(0.02)
        assert info["status"] == "ready", info
        path = recording.root / "daily-thumbnails" / (key + ".jpg")
        assert path.exists()
        await service.close()  # No async notification cleanup can conceal a parent-delete leak.
        await exports.delete_day(key)
        assert not path.exists()
        assert (await daily.get_day(key))["cleanup_state"] == "complete"
    finally:
        await service.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_expiry_denies_info_and_image_and_unknown_camera_uses_404(tmp_path):
    from camera_media_service.thumbnails import ThumbnailService

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = ThumbnailService(recording)
    await service.start()
    try:
        await service.prepare("clip", key)
        assert (await finished(service, "clip", key))["status"] == "ready"
        recording.cameras.clear()
        with pytest.raises(ApiError) as error:
            await service.info("clip", key)
        assert error.value.status == 404
        recording.cameras.add("camera-main")
        body["expires_at"] = iso(time.time() - 1)
        with recording.db:
            recording._save(body)
        for method in (service.info, service.open_image):
            with pytest.raises(ApiError) as error:
                await method("clip", key)
            assert error.value.status == 410
        assert not (recording.root / "clip-thumbnails" / (key + ".jpg")).exists()
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_repeated_shutdown_cancellation_does_not_release_pin_early(tmp_path, monkeypatch):
    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = thumbnails.ThumbnailService(recording)
    await service.start()
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            draining.set()
            await release.wait()

    monkeypatch.setattr(thumbnails, "generate_thumbnail", blocked)
    close = None
    try:
        await service.prepare("clip", key)
        await entered.wait()
        close = asyncio.create_task(service.close())
        await draining.wait()
        close.cancel()
        await asyncio.sleep(0.02)
        assert key in recording.thumbnail_pins and not close.done()
        release.set()
        await close
        assert not recording.thumbnail_pins and not recording.capacity_reservations
        assert not list((recording.root / "clip-thumbnails").glob("*.tmp.jpg"))
    finally:
        release.set()
        if close:
            await asyncio.gather(close, return_exceptions=True)
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_cleanup_io_failure_does_not_leave_pending_or_hold_source(tmp_path, monkeypatch):
    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = thumbnails.ThumbnailService(recording)
    await service.start()

    async def failed(*args, **kwargs):
        raise TimeoutError()

    async def denied(*args, **kwargs):
        raise PermissionError("image cleanup denied")

    monkeypatch.setattr(thumbnails, "generate_thumbnail", failed)
    monkeypatch.setattr(service, "_purge", denied)
    try:
        await service.prepare("clip", key)
        result = await finished(service, "clip", key)
        assert result["status"] == "failed" and not recording.thumbnail_pins
        assert (await recording.get_clip(key))["status"] == "ready"
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_corrupt_optional_pending_record_cannot_prevent_media_start(tmp_path):
    import sqlite3

    from aiohttp.test_utils import TestClient, TestServer

    from camera_media_service.http_api import create_http_app

    recording, body = await ready_clip(tmp_path)
    with sqlite3.connect(tmp_path / "thumbnails.sqlite3") as db:
        db.execute(
            "CREATE TABLE thumbnails(kind TEXT,id TEXT,state TEXT,body TEXT,PRIMARY KEY(kind,id))"
        )
        db.execute(
            "INSERT INTO thumbnails VALUES(?,?,?,?)",
            ("clip", body["clip_id"], "pending", "broken-json"),
        )
    async with TestClient(TestServer(create_http_app(recording))) as client:
        assert (await client.get("/api/v1/cameras/camera-main/recording-policy")).status == 200
        assert (await client.get("/api/v1/clips/" + body["clip_id"] + "/media-info")).status == 200


@pytest.mark.asyncio
async def test_stale_info_cannot_erase_a_newly_published_image(tmp_path, monkeypatch):
    from camera_media_service.thumbnails import ThumbnailService

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = ThumbnailService(recording)
    await service.start()
    entered, release = asyncio.Event(), asyncio.Event()
    stale = None
    first = True
    original = service.store.get

    async def gated(kind, identifier):
        nonlocal first
        value = await original(kind, identifier)
        if asyncio.current_task() is stale and first:
            first = False
            entered.set()
            await release.wait()
        return value

    try:
        await service.prepare("clip", key)
        assert (await finished(service, "clip", key))["status"] == "ready"
        path = recording.clips / (key + ".mp4")
        other = path.with_suffix(".replacement")
        shutil.copyfile(path, other)
        other.replace(path)
        monkeypatch.setattr(service.store, "get", gated)
        stale = asyncio.create_task(service.info("clip", key))
        await entered.wait()
        assert (await service.info("clip", key))["status"] == "unavailable"
        await service.prepare("clip", key)
        fresh = await finished(service, "clip", key)
        assert fresh["status"] == "ready"
        release.set()
        await stale
        assert await service.info("clip", key) == fresh
        assert (recording.root / "clip-thumbnails" / (key + ".jpg")).exists()
    finally:
        release.set()
        if stale:
            await asyncio.gather(stale, return_exceptions=True)
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_corrupt_persisted_identifier_cannot_delete_other_images(tmp_path):
    import json
    import sqlite3

    from camera_media_service.thumbnails import ThumbnailService

    recording, _ = await ready_clip(tmp_path)
    outside = tmp_path / "keep-original.jpg"
    outside.write_bytes(b"keep this synthetic source")
    invalid = str(outside.with_suffix(""))
    with sqlite3.connect(tmp_path / "thumbnails.sqlite3") as db:
        db.execute(
            "CREATE TABLE thumbnails(kind TEXT,id TEXT,state TEXT,body TEXT,PRIMARY KEY(kind,id))"
        )
        db.execute(
            "INSERT INTO thumbnails VALUES(?,?,?,?)",
            (
                "clip",
                invalid,
                "pending",
                json.dumps({"clip_id": invalid, "camera_id": "camera-main", "status": "pending"}),
            ),
        )
    service = ThumbnailService(recording)
    try:
        await service.start()
        assert outside.read_bytes() == b"keep this synthetic source"
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_result_persistence_failure_recovers_pending_without_restarting(
    tmp_path, monkeypatch
):
    import sqlite3

    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = thumbnails.ThumbnailService(recording)
    await service.start()
    original = service.store.put

    async def fail_result(kind, key, row):
        if row["status"] in ("ready", "failed"):
            raise sqlite3.OperationalError("temporary result IO failure")
        await original(kind, key, row)

    monkeypatch.setattr(service.store, "put", fail_result)
    try:
        await service.prepare("clip", key)
        for _ in range(200):
            if not service.jobs:
                break
            await asyncio.sleep(0.02)
        monkeypatch.setattr(service.store, "put", original)
        assert (await service.info("clip", key))["status"] == "failed"
        assert (await recording.get_clip(key))["status"] == "ready"
    finally:
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_generator_accepts_real_black_frame_rejects_missing_frame_and_oversize(tmp_path):
    import subprocess

    from PIL import Image, ImageStat

    from camera_media_service.thumbnail_media import ThumbnailFailure, generate_thumbnail

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "black.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=320x240:d=0.1",
            "-threads",
            "1",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    output = tmp_path / "image.jpg"
    await generate_thumbnail(ffmpeg, source, output)
    with Image.open(output) as image:
        assert max(ImageStat.Stat(image.convert("RGB")).mean) < 5
    with pytest.raises(ThumbnailFailure) as error:
        await generate_thumbnail(ffmpeg, source, output, max_bytes=32)
    assert error.value.reason == "IMAGE_TOO_LARGE" and not output.exists()
    source.write_bytes(b"not a video")
    with pytest.raises(ThumbnailFailure) as error:
        await generate_thumbnail(ffmpeg, source, output)
    assert error.value.reason == "NO_VIDEO_FRAME" and not output.exists()


@pytest.mark.asyncio
async def test_generator_timeout_reaps_os_child_and_removes_partial_image(tmp_path, monkeypatch):
    import sys

    from camera_media_service import thumbnail_media
    from camera_media_service.encoding import start_media_process

    child = None
    output = tmp_path / "partial.jpg"

    async def sleeping_child(*args, **kwargs):
        nonlocal child
        output.write_bytes(b"incomplete")
        child = await start_media_process(
            sys.executable, "-c", "import time; time.sleep(120)", **kwargs
        )
        return child

    monkeypatch.setattr(thumbnail_media, "start_media_process", sleeping_child)
    with pytest.raises(TimeoutError):
        await thumbnail_media.generate_thumbnail("unused", tmp_path / "source", output, timeout=0.2)
    assert child is not None and child.returncode is not None and not output.exists()


@pytest.mark.asyncio
async def test_source_replaced_while_generating_never_publishes_old_thumbnail(
    tmp_path, monkeypatch
):
    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    key = body["clip_id"]
    service = thumbnails.ThumbnailService(recording)
    await service.start()
    entered, release = asyncio.Event(), asyncio.Event()
    generate = thumbnails.generate_thumbnail

    async def held(*args, **kwargs):
        result = await generate(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(thumbnails, "generate_thumbnail", held)
    try:
        await service.prepare("clip", key)
        await entered.wait()
        path = recording.clips / (key + ".mp4")
        replacement = path.with_suffix(".replacement")
        shutil.copyfile(path, replacement)
        replacement.replace(path)
        release.set()
        result = await finished(service, "clip", key)
        assert result["status"] == "unavailable" and result["content_path"] is None
        await asyncio.gather(*list(service.jobs.values()), return_exceptions=True)
        assert not service._path("clip", key).exists()
        assert not recording.thumbnail_pins and not recording.capacity_reservations
        assert (await recording.get_clip(key))["status"] == "ready"
    finally:
        release.set()
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_cancel_during_prepare_prior_lookup_leaks_no_storage_reservation(
    tmp_path, monkeypatch
):
    from camera_media_service.thumbnails import ThumbnailService

    recording, body = await ready_clip(tmp_path)
    service = ThumbnailService(recording)
    await service.start()
    original = service.store.get
    entered = asyncio.Event()
    calls = 0

    async def held(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await asyncio.Future()
        return await original(*args)

    monkeypatch.setattr(service.store, "get", held)
    task = asyncio.create_task(service.prepare("clip", body["clip_id"]))
    try:
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not recording.capacity_reservations and not recording.capacity_outputs
        assert not service.jobs and not service.admitting
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
        await recording.close()


@pytest.mark.asyncio
async def test_delete_excludes_reprepare_until_parent_is_deleted(tmp_path, monkeypatch):
    from camera_media_service import thumbnails

    recording, body = await ready_clip(tmp_path)
    service = thumbnails.ThumbnailService(recording)
    await service.start()
    key = body["clip_id"]
    entered, drained, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_release = service._release
    deletion = retry = None

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    async def gated_release(*args):
        if deletion and not service.jobs:
            drained.set()
            await release.wait()
        return await original_release(*args)

    monkeypatch.setattr(thumbnails, "generate_thumbnail", blocked)
    monkeypatch.setattr(service, "_release", gated_release)
    try:
        await service.prepare("clip", key)
        await entered.wait()
        deletion = asyncio.create_task(recording.delete_clip(key))
        await asyncio.wait_for(drained.wait(), 2)
        entered.clear()
        retry = asyncio.create_task(service.prepare("clip", key, retry_failed=True))
        await asyncio.sleep(0.05)
        assert not entered.is_set() and not retry.done()
        release.set()
        await deletion
        with pytest.raises(ApiError) as error:
            await retry
        assert error.value.status == 410
        assert not recording.thumbnail_pins and not recording.capacity_reservations
    finally:
        release.set()
        if deletion:
            await asyncio.gather(deletion, return_exceptions=True)
        if retry:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        await service.close()
        await recording.close()
