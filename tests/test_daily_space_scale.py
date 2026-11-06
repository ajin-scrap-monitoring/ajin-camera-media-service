import json
from datetime import date

import pytest
from test_daily_recording import make_daily

from camera_media_service.daily_models import DailyWindow


@pytest.mark.asyncio
async def test_workspace_admission_does_not_scan_archived_segment_history(tmp_path, monkeypatch):
    start = DailyWindow.from_date(date(2026, 11, 6)).start_at
    recording, daily = make_daily(tmp_path, [start], monkeypatch)
    try:
        await daily.tick(start)
        old = daily.catalog.ensure_day("camera-main", DailyWindow.from_date(date(2026, 11, 5)), 30)
        old.update(status="closed", archive_state="mp4")
        with recording.db:
            daily.catalog.save_day(old)
            recording.db.executemany(
                "INSERT INTO daily_segments VALUES(?,?,?,?,?,?)",
                (
                    (
                        str(i),
                        old["recording_id"],
                        start - 10,
                        start - 9,
                        "ready",
                        json.dumps({"size_bytes": 1}),
                    )
                    for i in range(10000)
                ),
            )
        calls = []

        def extract(raw, path):
            calls.append(path)
            return json.loads(raw).get(path[2:])

        recording.db.create_function("json_extract", 2, extract)
        daily.settings.daily_archive_workspace_bytes = 1
        daily.settings.daily_archive_enabled = True
        await daily.check_space(recording=True)
        assert len(calls) < 10
    finally:
        daily.initialized = False
        await daily.close()
        await recording.close()
