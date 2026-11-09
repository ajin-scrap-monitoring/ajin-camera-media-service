import logging
from datetime import datetime

from camera_media_service.recording_diagnostics import RecordingDiagnostics


def test_archive_phase_records_utc_start_and_completion_with_wall_elapsed(caplog):
    stats = RecordingDiagnostics()
    with caplog.at_level(logging.INFO):
        with stats.measure("archive_verify"):
            pass
    records = [r for r in caplog.records if r.getMessage().startswith("Archive phase ")]
    assert len(records) == 2
    assert records[0].phase_event == "started"
    assert records[1].phase_event == "completed"
    assert records[0].phase_started_at == records[1].phase_started_at
    assert records[1].phase_ended_at.endswith("Z")
    assert datetime.fromisoformat(
        records[1].phase_ended_at.replace("Z", "+00:00")
    ) >= datetime.fromisoformat(records[0].phase_started_at.replace("Z", "+00:00"))
    assert (
        records[1].elapsed_seconds == stats.snapshot()["stages"]["archive_verify"]["total_seconds"]
    )
