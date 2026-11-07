import pytest
from test_daily_archive import archive_ready
from test_daily_export import make_exports, recorded_day


@pytest.mark.asyncio
async def test_archive_mappings_are_bounded_and_ready_is_published_last(tmp_path, monkeypatch):
    import camera_media_service.daily_export as module
    from camera_media_service.persisted_media_info import file_identity

    recording, daily, clock, day = await recorded_day(tmp_path, monkeypatch)
    key = day["recording_id"]
    first = daily.catalog.segments(key, ready_only=True)[0]
    with recording.db:
        for index in range(192):
            part = daily.catalog.begin_segment(
                key, first["start_at"] + index / 1000, first["end_at"]
            )
            daily.catalog.complete_segment(
                part["segment_id"],
                {**first, "segment_id": part["segment_id"], "start_at": part["start_at"]},
                persist_measurement=False,
            )
    expected = len(daily.catalog.segments(key, ready_only=True))
    commits, pending, published_after = [], set(), []

    def trace(sql):
        if sql.startswith("UPDATE daily_segments SET body="):
            pending.add(sql)
        if "daily_states" in sql and '"archive_state": "mp4"' in sql:
            published_after.append(sum(commits) + len(pending))
        if sql == "COMMIT":
            commits.append(len(pending))
            pending.clear()

    async def merge(ffmpeg, segments, temporary, final):
        final.parent.mkdir(exist_ok=True)
        final.write_bytes(b"owned fake mux boundary; encoding verified separately")

    async def verify(ffmpeg, path, segments):
        return {
            "size_bytes": path.stat().st_size,
            "_file_identity": file_identity(path.stat()),
            "media_info": {"status": "ready", "codec": "h264"},
        }

    monkeypatch.setattr(module, "merge_daily_mp4", merge)
    monkeypatch.setattr(module, "verify_archive", verify)
    recording.db.set_trace_callback(trace)
    exports = make_exports(daily)
    try:
        await exports.maintenance(clock[0])
        assert (await archive_ready(exports, daily, key))["export_state"] == "ready"
        assert sum(commits) == expected
        assert max(commits) <= 64, "A whole-day mapping transaction blocked the frame writer"
        assert published_after and all(count == expected for count in published_after)
    finally:
        recording.db.set_trace_callback(None)
        await exports.close()
        await daily.close()
        await recording.close()
