from camera_media_service.retention_assessment import assess_retention


def test_thirty_days_capacity_warning_does_not_change_policy():
    policies = {"camera-main": {"enabled": True, "retention_days": 30}}
    samples = {
        "camera-main": {
            "recording_id": "sample",
            "size_bytes": 8_000_000_000,
            "recorded_seconds": 86400,
        }
    }
    limits = {"daily_limit_bytes": 45_000_000_000, "media_limit_bytes": 51_000_000_000}
    result = assess_retention(
        policies,
        samples,
        limits,
        workspace=10_000_000_000,
        buffer=2_000_000_000,
        minimum_free=5_000_000_000,
        filesystem_total=63_000_000_000,
    )
    assert result["status"] == "insufficient_capacity"
    assert result["estimated_retention_bytes"] == 240_000_000_000
    assert result["required_filesystem_bytes_lower_bound"] == 257_000_000_000
    assert result["limiting_conditions"] == ["daily_budget", "media_budget", "filesystem"]
    assert policies["camera-main"]["retention_days"] == 30


def test_unknown_camera_or_partial_sample_cannot_prove_capacity_sufficient():
    policies = {
        "a": {"enabled": True, "retention_days": 30},
        "b": {"enabled": True, "retention_days": 30},
    }
    samples = {"a": {"recording_id": "short", "size_bytes": 100, "recorded_seconds": 10}}
    result = assess_retention(
        policies, samples, {}, workspace=0, buffer=0, minimum_free=0, filesystem_total=None
    )
    assert result["status"] == "unknown"
    assert result["estimated_retention_bytes"] is None
    assert all(c["estimated_daily_bytes"] is None for c in result["cameras"])
