from __future__ import annotations

import pytest

from camera_media_service.settings import Settings, SettingsError


def test_daily_feature_requires_finite_storage_budget_and_parses_profile():
    env = {"EDGE_AUTH_SECRET": '{"cam-1":"test"}', "DAILY_RECORDING_ENABLED": "true"}
    with pytest.raises(SettingsError, match="MAX_DAILY_STORAGE_BYTES"):
        Settings.from_env(env)
    env.update(
        MAX_DAILY_STORAGE_BYTES="1073741824", DAILY_WIDTH="1280", DAILY_HEIGHT="720", DAILY_FPS="15"
    )
    settings = Settings.from_env(env)
    assert settings.daily_recording_enabled and settings.max_daily_storage_bytes == 1073741824
    assert (
        settings.daily_width == 1280 and settings.daily_height == 720 and settings.daily_fps == 15
    )
    for key, value in [
        ("DAILY_FPS", "0"),
        ("DAILY_WIDTH", "641"),
        ("DAILY_RECORDING_ENABLED", "perhaps"),
    ]:
        with pytest.raises(SettingsError):
            Settings.from_env({**env, key: value})


def test_settings_from_env_rejects_documented_default_without_auth_secret() -> None:
    with pytest.raises(SettingsError, match="EDGE_AUTH_SECRET must not be empty"):
        Settings.from_env({})


def test_settings_from_env_parses_valid_values_without_bypassing_auth() -> None:
    settings = Settings.from_env(
        {
            "MEDIA_INGEST_HOST": "127.0.0.1",
            "MEDIA_INGEST_PORT": "9443",
            "MEDIA_INGEST_PATH": "/stream/{camera_id}",
            "MEDIA_STORAGE_PATH": "/tmp/camera-media",
            "MAX_FRAME_BYTES": "1048576",
            "STATS_INTERVAL_SECONDS": "30",
            "LOG_FORMAT": "json",
            "EDGE_AUTH_SECRET": '{"cam-1":"token-1"}',
            "TLS_CERT_PATH": "",
            "TLS_KEY_PATH": "",
        }
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 9443
    assert settings.route_path == "/stream/{camera_id}"
    assert settings.media_root == "/tmp/camera-media"
    assert settings.max_frame_bytes == 1048576
    assert settings.stats_interval_seconds == 30
    assert settings.log_format == "json"
    assert settings.camera_tokens == {"cam-1": "token-1"}


def test_settings_from_env_accepts_a_complete_direct_tls_configuration() -> None:
    settings = Settings.from_env(
        {
            "EDGE_AUTH_SECRET": '{"cam-1":"test-token"}',
            "TLS_CERT_PATH": "/run/secrets/cert.pem",
            "TLS_KEY_PATH": "/run/secrets/key.pem",
        }
    )

    assert settings.server_cert_path == "/run/secrets/cert.pem"
    assert settings.server_key_path == "/run/secrets/key.pem"


def test_settings_direct_construction_remains_available_for_tests() -> None:
    settings = Settings(camera_tokens={"cam-1": "test-token"})

    assert settings.camera_tokens == {"cam-1": "test-token"}


def test_settings_rejects_duplicate_camera_id_placeholders() -> None:
    with pytest.raises(SettingsError):
        Settings.from_env({"MEDIA_INGEST_PATH": "/api/v1/cameras/{camera_id}/feeds/{camera_id}"})


@pytest.mark.parametrize(
    "environ",
    [
        {"EDGE_AUTH_SECRET": "   "},
        {"MEDIA_INGEST_HOST": ""},
        {"MEDIA_INGEST_PORT": "0"},
        {"MEDIA_INGEST_PORT": "not-a-number"},
        {"MEDIA_INGEST_PATH": "api/v1/cameras/{camera_id}/stream"},
        {"MEDIA_INGEST_PATH": "/api/v1/cameras/cam-1/stream"},
        {"MEDIA_STORAGE_PATH": ""},
        {"MAX_FRAME_BYTES": "0"},
        {"STATS_INTERVAL_SECONDS": "-1"},
        {"LOG_FORMAT": "plain"},
        {"EDGE_AUTH_SECRET": "not-json"},
        {"EDGE_AUTH_SECRET": "[]"},
        {"EDGE_AUTH_SECRET": '{"cam-1":""}'},
        {"TLS_CERT_PATH": "/tmp/cert.pem"},
        {"TLS_KEY_PATH": "/tmp/key.pem"},
    ],
)
def test_settings_from_env_rejects_invalid_values(environ: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        Settings.from_env(environ)
