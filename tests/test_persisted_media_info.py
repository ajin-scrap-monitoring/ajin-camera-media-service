import asyncio
import os
import shutil
import sqlite3
from types import SimpleNamespace

import imageio_ffmpeg
import pytest
from test_media_info import make_video

from camera_media_service import media_details
from camera_media_service.persisted_media_info import close_probe_database


class ProbeOwnerConnection(sqlite3.Connection):
    def close(self):
        close_probe_database(self)
        super().close()


def details_for(root, *, ffmpeg=None):
    db = sqlite3.connect(root / "catalog.sqlite3", factory=ProbeOwnerConnection)
    return media_details.MediaDetails(
        SimpleNamespace(
            db=db, root=root, lock=asyncio.Lock(), ffmpeg=ffmpeg or imageio_ffmpeg.get_ffmpeg_exe()
        )
    )


@pytest.mark.asyncio
async def test_actual_probe_survives_restart_without_starting_ffmpeg(tmp_path):
    path = make_video(tmp_path, width=320, height=180, rate="30000/1001")
    first = details_for(tmp_path)
    assert (await first.inspect(path))["width"] == 320
    first.recording.db.close()
    restarted = details_for(tmp_path, ffmpeg="missing-ffmpeg-after-restart")
    try:
        measured = await restarted.inspect(path)
        assert measured["status"] == "ready"
        assert measured["width"] == 320
        assert measured["fps_num"] == 30000
        assert measured["fps_den"] == 1001
    finally:
        restarted.recording.db.close()


@pytest.mark.asyncio
async def test_actual_probe_survives_memory_cache_eviction(tmp_path):
    fixture = make_video(tmp_path)
    details = details_for(tmp_path)
    paths = [tmp_path / f"{index}.mp4" for index in range(129)]
    try:
        for path in paths:
            shutil.copyfile(fixture, path)
            assert (await details.inspect(path))["status"] == "ready"
        details.recording.ffmpeg = "missing-ffmpeg-after-eviction"
        assert (await details.inspect(paths[0]))["width"] == 160
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt", reason="Windows stat ctime is creation time; Linux uses change time"
)
async def test_same_inode_size_and_mtime_replacement_invalidates_persisted_probe(tmp_path):
    path = make_video(tmp_path)
    original = path.read_bytes()
    alternate_dir = tmp_path / "alternate"
    alternate_dir.mkdir()
    alternate = make_video(alternate_dir, width=320, height=180).read_bytes()
    size = max(len(original), len(alternate))
    path.write_bytes(original.ljust(size, b"\0"))
    details = details_for(tmp_path)
    try:
        assert (await details.inspect(path))["width"] == 160
        initial = path.stat()
        await asyncio.sleep(0.01)
        path.write_bytes(alternate.ljust(size, b"\0"))
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        assert path.stat().st_ino == initial.st_ino
        assert (await details.inspect(path))["width"] == 320
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_probe_result_is_not_adopted_if_file_changes_after_measurement(tmp_path, monkeypatch):
    path = make_video(tmp_path)
    alternate_dir = tmp_path / "alternate"
    alternate_dir.mkdir()
    replacement = make_video(alternate_dir, width=320, height=180)
    original = media_details.inspect_media_info

    async def replaced(ffmpeg, path):
        measured = await original(ffmpeg, path)
        os.replace(replacement, path)
        return measured

    details = details_for(tmp_path)
    try:
        monkeypatch.setattr(media_details, "inspect_media_info", replaced)
        measured = await details.inspect(path)
        assert measured["status"] == "unknown"
        assert measured["reason_code"] == "file_changed"
        monkeypatch.setattr(media_details, "inspect_media_info", original)
        assert (await details.inspect(path))["width"] == 320
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_duplicate_waiters_share_one_probe_and_one_cancellation_does_not_cancel_it(
    tmp_path, monkeypatch
):
    path = make_video(tmp_path)
    details = details_for(tmp_path)
    original = media_details.inspect_media_info
    entered, release = asyncio.Event(), asyncio.Event()
    probes = 0

    async def blocked(ffmpeg, path):
        nonlocal probes
        probes += 1
        entered.set()
        await release.wait()
        return await original(ffmpeg, path)

    monkeypatch.setattr(media_details, "inspect_media_info", blocked)
    first = asyncio.create_task(details.inspect(path))
    await asyncio.wait_for(entered.wait(), 2)
    others = [asyncio.create_task(details.inspect(path)) for _ in range(12)]
    try:
        await asyncio.sleep(0.1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        results = await asyncio.gather(*others)
        assert all(info["width"] == 160 for info in results)
        assert probes == 1
        assert details.active == 0
    finally:
        release.set()
        await asyncio.gather(first, *others, return_exceptions=True)
        details.recording.db.close()


@pytest.mark.asyncio
async def test_generated_daily_segment_is_persisted_before_first_metadata_request(
    tmp_path, monkeypatch
):
    from test_daily_export import recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    try:
        segment = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        recording.ffmpeg = "missing-ffmpeg-after-generation"
        measured = await media_details.MediaDetails(recording).inspect(tmp_path / segment["path"])
        assert measured["status"] == "ready"
        assert measured["width"] == 320
    finally:
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_generated_archive_is_persisted_before_first_metadata_request(tmp_path, monkeypatch):
    from test_daily_export import make_exports, recorded_day, wait_ready

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        await exports.maintenance(clock[0])
        assert (await wait_ready(exports, daily, day["recording_id"]))["export_state"] == "ready"
        recording.ffmpeg = "missing-ffmpeg-after-generation"
        measured = await media_details.MediaDetails(recording).inspect(
            exports._path(day["recording_id"])
        )
        assert measured["status"] == "ready"
        assert measured["fps"] == 30
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_generated_range_clip_is_persisted_before_first_metadata_request(
    tmp_path, monkeypatch
):
    from test_daily_export import recorded_day

    from camera_media_service.recorded_clips import RecordedClipService
    from camera_media_service.recording import iso

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    clips = RecordedClipService(recording, daily)
    try:
        segment = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        body, _ = await clips.create_range(
            "camera-main",
            "persisted-range",
            iso(segment["start_at"] + 0.1),
            iso(segment["start_at"] + 0.7),
        )
        await asyncio.gather(*clips.jobs.values())
        assert recording._get(body["clip_id"])["status"] == "ready"
        recording.ffmpeg = "missing-ffmpeg-after-generation"
        measured = await media_details.MediaDetails(recording).clip(body["clip_id"])
        assert measured["media_info"]["status"] == "ready"
        assert measured["media_info"]["fps"] == 30
    finally:
        await clips.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_generated_jpeg_clip_is_persisted_before_first_metadata_request(tmp_path):
    import time

    from test_daily_recording import jpeg
    from test_recording import make_service

    from camera_media_service.pipeline import FrameEnvelope

    recording = make_service(tmp_path)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        await recording.set_policy(
            "camera-main", {"pre_roll_seconds": 5, "post_roll_seconds": 0, "retention_days": 1}
        )
        now = time.time()
        for index in range(5):
            await recording.store_frame(
                FrameEnvelope("camera-main", now - 1 + index / 10, index, jpeg())
            )
        body, _ = await recording.create_clip("camera-main", "persisted-jpeg")
        await recording.process_clip(body["clip_id"])
        assert recording._get(body["clip_id"])["status"] == "ready"
        recording.ffmpeg = "missing-ffmpeg-after-generation"
        measured = await media_details.MediaDetails(recording).clip(body["clip_id"])
        assert measured["media_info"]["status"] == "ready"
        assert measured["media_info"]["width"] == 320
    finally:
        await recording.close()


@pytest.mark.asyncio
async def test_successful_legacy_segment_sample_advances_to_next_missing_segment(
    tmp_path, monkeypatch
):
    import json

    from test_daily_export import make_exports, recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        original = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        with recording.db:
            original.pop("media_info", None)
            recording.db.execute(
                "UPDATE daily_segments SET body=? WHERE id=?",
                (json.dumps(original), original["segment_id"]),
            )
            duplicate = daily.catalog.begin_segment(
                day["recording_id"], original["end_at"], original["end_at"] + original["duration"]
            )
            path = daily.segment_path(duplicate)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp_path / original["path"], path)
            daily.catalog.complete_segment(
                duplicate["segment_id"],
                {
                    "duration": original["duration"],
                    "path": str(path.relative_to(tmp_path)),
                    "size_bytes": path.stat().st_size,
                    "profile": original["profile"],
                },
            )
        details = media_details.MediaDetails(recording, daily, exports)
        with sqlite3.connect(tmp_path / "media-info.sqlite3") as metadata_db:
            metadata_db.execute("DELETE FROM measured_media_files")
        first = await details.day(day["recording_id"])
        assert first["segments"]["status"] == "partial"
        assert first["segments"]["measured"] == 1
        second = await details.day(day["recording_id"])
        assert second["segments"]["status"] == "ready"
        assert second["segments"]["measured"] == 2
        assert second["segments"]["mixed"] is False
        assert recording.db.execute("SELECT count(*) FROM clip_recording_pins").fetchone()[0] == 0
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive", [False, True])
async def test_generation_verification_rejects_identity_change_during_probe(
    tmp_path, monkeypatch, archive
):
    from camera_media_service import encoding

    path = make_video(tmp_path)
    replacement = tmp_path / "replacement.mp4"
    shutil.copyfile(path, replacement)
    spawn = asyncio.create_subprocess_exec

    async def replacing(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        communicate = child.communicate

        async def replaced(*args, **kwargs):
            result = await communicate(*args, **kwargs)
            os.replace(replacement, path)
            return result

        child.communicate = replaced
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", replacing)
    with pytest.raises(RuntimeError, match="changed"):
        if archive:
            await encoding.verify_archive(
                imageio_ffmpeg.get_ffmpeg_exe(), path, [{"duration": 0.5, "profile": {"fps": 10}}]
            )
        else:
            await encoding.probe_media(imageio_ffmpeg.get_ffmpeg_exe(), path)


@pytest.mark.asyncio
async def test_day_aggregate_does_not_reuse_replaced_segment_measurement(tmp_path, monkeypatch):
    from test_daily_export import make_exports, recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        segment = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        alternate_dir = tmp_path / "alternate"
        alternate_dir.mkdir()
        replacement = make_video(alternate_dir, width=640, height=360, rate="10", suffix=".ts")
        os.replace(replacement, tmp_path / segment["path"])
        result = await media_details.MediaDetails(recording, daily, exports).day(
            day["recording_id"]
        )
        assert result["segments"]["measured"] == 1
        assert result["segments"]["formats"][0]["width"] == 640
        assert result["segments"]["formats"][0]["fps"] == 10
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [False, True])
async def test_persisted_read_checks_identity_after_lookup(tmp_path, monkeypatch, removed):
    path = make_video(tmp_path)
    replacement = tmp_path / "replacement.mp4"
    shutil.copyfile(path, replacement)
    first = details_for(tmp_path)
    assert (await first.inspect(path))["status"] == "ready"
    first.recording.db.close()
    details = details_for(tmp_path)
    load = media_details.load_measured_info

    def changed(*args):
        info = load(*args)
        if removed:
            path.unlink()
        else:
            os.replace(replacement, path)
        return info

    monkeypatch.setattr(media_details, "load_measured_info", changed)
    try:
        info = await details.inspect(path)
        assert info["status"] == "unknown"
        assert info["reason_code"] == ("file_unavailable" if removed else "file_changed")
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_failed_persisted_probe_remains_retryable_for_unchanged_file(tmp_path):
    path = make_video(tmp_path)
    details = details_for(tmp_path, ffmpeg="temporarily-unavailable-ffmpeg")
    try:
        assert (await details.inspect(path))["status"] == "unknown"
        details.recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        assert (await details.inspect(path))["width"] == 160
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_unattested_legacy_generation_metadata_is_not_adopted(tmp_path):
    from camera_media_service.encoding import probe_media
    from camera_media_service.persisted_media_info import record_generated_info

    path = make_video(tmp_path)
    metadata = await probe_media(imageio_ffmpeg.get_ffmpeg_exe(), path)
    metadata.pop("_file_identity", None)
    details = details_for(tmp_path, ffmpeg="missing-ffmpeg")
    try:
        with details.recording.db:
            record_generated_info(details.recording.db, tmp_path, path, metadata)
        assert (await details.inspect(path))["status"] == "unknown"
    finally:
        details.recording.db.close()


@pytest.mark.asyncio
async def test_changed_generation_file_is_not_published_under_old_measurement(tmp_path):
    from camera_media_service.encoding import probe_media
    from camera_media_service.persisted_media_info import publish_generated_info

    path = make_video(tmp_path)
    metadata = await probe_media(imageio_ffmpeg.get_ffmpeg_exe(), path)
    path.write_bytes(path.read_bytes() + b"changed-after-probe")
    output = tmp_path / "ready.mp4"
    with pytest.raises(RuntimeError, match="changed"):
        await publish_generated_info(metadata, path, output)
    assert not output.exists()
    assert path.exists()


@pytest.mark.asyncio
async def test_recovered_segment_persists_fresh_probe_identity(tmp_path, monkeypatch):
    import json

    from test_daily_export import recorded_day

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    try:
        segment = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        with recording.db:
            segment.update(status="processing")
            recording.db.execute(
                "UPDATE daily_segments SET status='processing',body=? WHERE id=?",
                (json.dumps(segment), segment["segment_id"]),
            )
        with sqlite3.connect(tmp_path / "media-info.sqlite3") as metadata_db:
            metadata_db.execute("DELETE FROM measured_media_files")
        await daily._recover_segments()
        assert len(daily.catalog.segments(day["recording_id"], ready_only=True)) == 1
        recording.ffmpeg = "missing-ffmpeg-after-recovery"
        result = await media_details.MediaDetails(recording).inspect(tmp_path / segment["path"])
        assert result["status"] == "ready"
        assert result["width"] == 320
        assert "_file_identity" not in daily.catalog.segments(day["recording_id"])[0]
    finally:
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_last_waiter_repeated_cancellation_drains_probe_cleanup(tmp_path, monkeypatch):
    path = make_video(tmp_path)
    details = details_for(tmp_path)
    entered, cleanup_started = asyncio.Event(), asyncio.Event()
    finish_cleanup, cleanup_done = asyncio.Event(), asyncio.Event()

    async def blocked(ffmpeg, path):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
            cleanup_done.set()

    monkeypatch.setattr(media_details, "inspect_media_info", blocked)
    owner = asyncio.create_task(details.inspect(path))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        owner.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 2)
        owner.cancel()
        await asyncio.sleep(0.05)
        assert not owner.done()
        assert details.active == 1
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert cleanup_done.is_set()
        assert details.active == 0
        assert not details.flights
    finally:
        finish_cleanup.set()
        await asyncio.gather(owner, return_exceptions=True)
        details.recording.db.close()


@pytest.mark.asyncio
async def test_probe_persistence_cannot_commit_pending_frame_transaction(tmp_path, monkeypatch):
    import threading
    from pathlib import Path

    from test_recording import make_service

    from camera_media_service.pipeline import FrameEnvelope

    recording = make_service(tmp_path)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    path = make_video(tmp_path)
    details = media_details.MediaDetails(recording)
    second_write, release_write = threading.Event(), threading.Event()
    probed = asyncio.Event()
    write = Path.write_bytes
    inspect = media_details.inspect_media_info
    writes = 0

    def fail_second(self, data):
        nonlocal writes
        if self.suffix == ".jpg":
            writes += 1
            if writes == 2:
                second_write.set()
                release_write.wait(5)
                raise OSError("second frame failed")
        return write(self, data)

    async def measured(ffmpeg, path):
        result = await inspect(ffmpeg, path)
        probed.set()
        return result

    monkeypatch.setattr(Path, "write_bytes", fail_second)
    monkeypatch.setattr(media_details, "inspect_media_info", measured)
    frames = asyncio.create_task(
        recording.store_frames(
            [
                FrameEnvelope("camera-main", 100, 0, b"one"),
                FrameEnvelope("camera-main", 100.1, 1, b"two"),
            ]
        )
    )
    metadata = None
    try:
        assert await asyncio.to_thread(second_write.wait, 3)
        metadata = asyncio.create_task(details.inspect(path))
        await asyncio.wait_for(probed.wait(), 3)
        await asyncio.sleep(0.05)
        release_write.set()
        with pytest.raises(OSError, match="second frame failed"):
            await frames
        assert (await metadata)["status"] == "ready"
        assert recording.db.execute("SELECT count(*) FROM frames").fetchone()[0] == 0
        assert not list(recording.buffer.glob("*.jpg"))
    finally:
        release_write.set()
        await asyncio.gather(frames, *([metadata] if metadata else []), return_exceptions=True)
        await recording.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ABORT", "ROLLBACK"])
async def test_optional_metadata_failure_does_not_fail_measurement_and_retries(tmp_path, action):
    from camera_media_service.persisted_media_info import ensure_probe_table

    path = make_video(tmp_path)
    details = details_for(tmp_path)
    try:
        with sqlite3.connect(tmp_path / "media-info.sqlite3") as metadata_db:
            ensure_probe_table(metadata_db)
            metadata_db.execute(
                "CREATE TRIGGER reject_measurement BEFORE INSERT ON "
                "measured_media_files BEGIN "
                f"SELECT RAISE({action},'optional metadata unavailable'); END"
            )
        assert (await details.inspect(path))["width"] == 160
        with sqlite3.connect(tmp_path / "media-info.sqlite3") as metadata_db:
            metadata_db.execute("DROP TRIGGER reject_measurement")
        assert (await details.inspect(path))["width"] == 160
        details.recording.db.close()
        restarted = details_for(tmp_path, ffmpeg="missing-ffmpeg-after-backfill")
        try:
            assert (await restarted.inspect(path))["width"] == 160
        finally:
            restarted.recording.db.close()
    finally:
        # sqlite close is idempotent.
        details.recording.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["segment", "archive", "extracted", "jpeg"])
@pytest.mark.parametrize("action", ["ABORT", "ROLLBACK"])
async def test_optional_metadata_insert_failure_does_not_block_ready_generation(
    tmp_path, monkeypatch, kind, action
):
    import json

    from test_daily_export import make_exports, recorded_day

    from camera_media_service.recorded_clips import RecordedClipService
    from camera_media_service.recording import iso

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports, clips = make_exports(daily), RecordedClipService(recording, daily)
    try:
        segment = daily.catalog.segments(day["recording_id"], ready_only=True)[0]
        with sqlite3.connect(tmp_path / "media-info.sqlite3") as metadata_db:
            metadata_db.execute(
                "CREATE TRIGGER reject_measurement BEFORE INSERT ON "
                "measured_media_files BEGIN "
                f"SELECT RAISE({action},'optional metadata unavailable'); END"
            )
        if kind == "segment":
            with recording.db:
                segment["status"] = "processing"
                recording.db.execute(
                    "UPDATE daily_segments SET status='processing',body=? WHERE id=?",
                    (json.dumps(segment), segment["segment_id"]),
                )
            await daily._recover_segments()
            assert daily.catalog.segments(day["recording_id"], ready_only=True)
        elif kind == "jpeg":
            await recording.set_policy(
                "camera-main",
                {
                    "pre_roll_seconds": 20,
                    "post_roll_seconds": 0,
                    "retention_days": 1,
                },
            )
            body, _ = await recording.create_clip("camera-main", "optional-jpeg-metadata")
            await asyncio.wait_for(recording.process_clip(body["clip_id"]), 3)
            assert recording._get(body["clip_id"])["status"] == "ready"
        elif kind == "archive":
            await exports._enqueue(day["recording_id"])
            await asyncio.wait_for(asyncio.gather(*exports.jobs.values()), 3)
            assert daily.catalog.get_day(day["recording_id"])["export_state"] == "ready"
            assert (
                recording.db.execute(
                    "SELECT status FROM daily_exports WHERE day_id=?", (day["recording_id"],)
                ).fetchone()[0]
                == "ready"
            )
        else:
            body, _ = await clips.create_range(
                "camera-main",
                "optional-metadata",
                iso(segment["start_at"] + 0.1),
                iso(segment["start_at"] + 0.7),
            )
            await asyncio.wait_for(asyncio.gather(*clips.jobs.values()), 3)
            assert recording._get(body["clip_id"])["status"] == "ready"
        await clips.close()
        await exports.close()
        await daily.close()
        await recording.close()
        from test_daily_recording import make_daily

        reopened, reopened_daily = make_daily(tmp_path, clock, monkeypatch)
        reopened_exports = make_exports(reopened_daily)
        try:
            if kind == "archive":
                assert (
                    reopened_daily.catalog.get_day(day["recording_id"])["export_state"] == "ready"
                )
            elif kind == "segment":
                assert reopened_daily.catalog.segments(day["recording_id"], ready_only=True)
            else:
                assert reopened._get(body["clip_id"])["status"] == "ready"
        finally:
            await reopened_exports.close()
            await reopened_daily.close()
            await reopened.close()
    finally:
        await clips.close()
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_archived_aggregate_requires_verified_current_master_identity(
    tmp_path, monkeypatch, legacy
):
    from test_daily_export import make_exports, recorded_day, wait_ready

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    exports = make_exports(daily)
    try:
        await exports.maintenance(clock[0])
        assert (await wait_ready(exports, daily, day["recording_id"]))["export_state"] == "ready"
        await exports.maintenance(clock[0])
        assert not list((tmp_path / "daily").rglob("*.ts"))
        initial = await media_details.MediaDetails(recording, daily, exports).day(
            day["recording_id"]
        )
        assert initial["segments"]["status"] == "ready"
        assert initial["segments"]["measured"] == 1
        if legacy:
            with sqlite3.connect(tmp_path / "media-info.sqlite3") as metadata_db:
                for (table,) in metadata_db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'verified_media_%'"
                ).fetchall():
                    metadata_db.execute("DELETE FROM " + table)
        else:
            alternate_dir = tmp_path / "alternate"
            alternate_dir.mkdir()
            replacement = make_video(alternate_dir, width=640, height=360, rate="10")
            os.replace(replacement, exports._path(day["recording_id"]))
        info = await media_details.MediaDetails(recording, daily, exports).day(day["recording_id"])
        assert info["file"]["status"] == "ready"
        assert info["segments"]["status"] == "unknown"
        assert info["segments"]["measured"] == 0
        assert info["segments"]["mixed"] is None
        assert info["segments"]["formats"] == []
        assert set(info) == {"recording_id", "file", "segments", "timestamp_info", "thumbnail"}
    finally:
        await exports.close()
        await daily.close()
        await recording.close()


@pytest.mark.asyncio
async def test_owner_reuses_private_connection_and_closes_it_with_service(tmp_path, monkeypatch):
    from test_recording import make_service

    from camera_media_service import persisted_media_info

    recording = make_service(tmp_path)
    recording.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    path = make_video(tmp_path)
    connect = sqlite3.connect
    connections = []

    def tracked(database, *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        if "media-info.sqlite3" in str(database):
            connections.append(connection)
        return connection

    monkeypatch.setattr(persisted_media_info.sqlite3, "connect", tracked)
    details = media_details.MediaDetails(recording)
    try:
        assert (await details.inspect(path))["width"] == 160
        for _ in range(100):
            assert (await details._stored_segment_info(path))["width"] == 160
        assert len(connections) == 1
    finally:
        await recording.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_valid_identity_legacy_catalog_measurement_remains_reusable(tmp_path):
    import json

    from camera_media_service.media_info import inspect_media_info
    from camera_media_service.persisted_media_info import ensure_probe_table, file_identity

    path = make_video(tmp_path)
    measured = await inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    details = details_for(tmp_path, ffmpeg="missing-ffmpeg-for-valid-legacy-cache")
    try:
        with details.recording.db:
            ensure_probe_table(details.recording.db)
            details.recording.db.execute(
                "INSERT INTO measured_media_files VALUES(?,?,?)",
                (
                    "fixture.mp4",
                    json.dumps(file_identity(path.stat())),
                    json.dumps(measured),
                ),
            )
        assert (await details.inspect(path))["width"] == 160
        assert not (tmp_path / "media-info.sqlite3").exists()
    finally:
        details.recording.db.close()
