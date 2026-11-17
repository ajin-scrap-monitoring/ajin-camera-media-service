import importlib.util

import pytest


def module():
    assert importlib.util.find_spec("camera_media_service.capacity_policy"), (
        "capacity policy missing"
    )
    from camera_media_service import capacity_policy

    return capacity_policy


def test_explicit_enable_and_invalid_config_never_enable_cleanup():
    policy = module().CleanupPolicy
    assert not policy.from_env({}).enabled
    value = policy.from_env({"MEDIA_CAPACITY_CLEANUP_ENABLED": "true"})
    assert value.enabled and value.stop_percent == 85 and value.start_percent == 90
    assert value.critical_percent == 95 and value.max_days == 1
    assert value.max_bytes == 16 * 1024**3
    for key, raw in [
        ("ENABLED", "yes"),
        ("START_PERCENT", "84"),
        ("INTERVAL_SECONDS", "0"),
        ("MAX_DAYS", "101"),
        ("MAX_BYTES", "0"),
        ("STOP_PERCENT", "nan"),
        ("RESET_ID", "x" * 65),
    ]:
        bad = policy.from_env(
            {"MEDIA_CAPACITY_CLEANUP_ENABLED": "true", "MEDIA_CAPACITY_CLEANUP_" + key: raw}
        )
        assert not bad.enabled and bad.configuration_error == "INVALID_CAPACITY_CLEANUP_CONFIG"
    assert not policy.from_env(
        {"MEDIA_CAPACITY_CLEANUP_ENABLED": "true", "MEDIA_CAPACITY_CLEANUP_START_PERCNET": "1"}
    ).enabled


def test_pressure_uses_admission_reservations_once_and_filesystem_reserve():
    budget = dict(
        daily_used_bytes=70,
        media_used_bytes=100,
        filesystem_free_bytes=100,
        filesystem_total_bytes=250,
        unallocated_reserved_bytes=5,
        additional_reserve_bytes=2,
        workspace_bytes=13,
        buffer_headroom_bytes=10,
        daily_limit_bytes=100,
        media_limit_bytes=200,
        minimum_free_bytes=20,
    )
    value = module().pressure_snapshot({"status": "available", "budget": budget})
    assert value["pressures"]["daily_budget"]["percent"] == 90
    assert value["pressures"]["total_budget"]["used_bytes"] == 130
    assert value["pressures"]["minimum_free"]["used_bytes"] == 200
    assert value["pressures"]["minimum_free"]["percent"] == 80
    assert value["pressures"]["daily_budget"]["shortfall_bytes"] == 0
    budget["workspace_bytes"] = 0
    assert (
        module().pressure_snapshot({"status": "available", "budget": budget})["pressures"][
            "daily_budget"
        ]["used_bytes"]
        == 77
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"status": "unknown", "budget": {}},
        {"status": "available", "budget": {"daily_used_bytes": float("nan")}},
    ],
)
def test_unknown_measurement_is_not_cleanup_authority(bad):
    with pytest.raises(ValueError):
        module().pressure_snapshot(bad)
