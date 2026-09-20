from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(slots=True)
class Settings:
    """Runtime settings; empty TLS paths mean internal reverse-proxy mode."""

    camera_tokens: dict[str, str]
    host: str = "0.0.0.0"
    port: int = 8443
    route_path: str = "/api/v1/cameras/{camera_id}/stream"
    media_root: str = "/var/lib/camera-media"
    max_frame_bytes: int = 4 * 1024 * 1024
    stats_interval_seconds: int = 60
    log_format: str = "json"
    web_application_url: str = ""
    web_application_credential: str = ""
    server_cert_path: str = ""
    server_key_path: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8889
    ffmpeg_path: str = "ffmpeg"
    max_buffer_bytes: int = 2 * 1024**3
    max_clip_jobs: int = 32

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        host = _read_required_text(environ, "MEDIA_INGEST_HOST", default="0.0.0.0")
        port = _read_int(environ, "MEDIA_INGEST_PORT", default=8443, minimum=1, maximum=65535)
        route_path = _read_required_text(
            environ,
            "MEDIA_INGEST_PATH",
            default="/api/v1/cameras/{camera_id}/stream",
        )
        if not route_path.startswith("/") or route_path.count("{camera_id}") != 1:
            raise SettingsError(
                "MEDIA_INGEST_PATH must start with '/' and contain exactly one '{camera_id}'"
            )

        media_root = _read_required_text(
            environ, "MEDIA_STORAGE_PATH", default="/var/lib/camera-media"
        )
        max_frame_bytes = _read_int(
            environ,
            "MAX_FRAME_BYTES",
            default=4 * 1024 * 1024,
            minimum=1,
        )
        stats_interval_seconds = _read_int(
            environ,
            "STATS_INTERVAL_SECONDS",
            default=60,
            minimum=1,
        )
        log_format = _read_required_text(environ, "LOG_FORMAT", default="json")
        if log_format != "json":
            raise SettingsError("LOG_FORMAT must be 'json'")

        web_application_url = _read_optional_empty_text(environ, "WEB_APPLICATION_URL")
        web_application_credential = _read_optional_empty_text(
            environ, "WEB_APPLICATION_CREDENTIAL"
        )
        server_cert_path = _read_optional_text(environ, "TLS_CERT_PATH")
        server_key_path = _read_optional_text(environ, "TLS_KEY_PATH")
        if bool(server_cert_path) != bool(server_key_path):
            raise SettingsError("TLS_CERT_PATH and TLS_KEY_PATH must be configured together")
        edge_auth_secret = _read_required_text(environ, "EDGE_AUTH_SECRET", default="")
        camera_tokens = _read_camera_tokens(edge_auth_secret)

        return cls(
            camera_tokens=camera_tokens,
            host=host,
            port=port,
            route_path=route_path,
            media_root=media_root,
            max_frame_bytes=max_frame_bytes,
            stats_interval_seconds=stats_interval_seconds,
            log_format=log_format,
            web_application_url=web_application_url,
            web_application_credential=web_application_credential,
            server_cert_path=server_cert_path,
            server_key_path=server_key_path,
            api_host=_read_required_text(environ, "MEDIA_API_HOST", "127.0.0.1"),
            api_port=_read_int(environ, "MEDIA_API_PORT", 8889, 1, 65535),
            ffmpeg_path=_read_required_text(environ, "FFMPEG_PATH", "ffmpeg"),
            max_buffer_bytes=_read_int(environ, "MAX_BUFFER_BYTES", 2 * 1024**3, 1),
            max_clip_jobs=_read_int(environ, "MAX_CLIP_JOBS", 32, 1),
        )


class SettingsError(ValueError):
    pass


def _read_required_text(environ: Mapping[str, str], key: str, default: str) -> str:
    value = environ.get(key, default).strip()
    if value == "":
        raise SettingsError(f"{key} must not be empty")
    return value


def _read_optional_text(environ: Mapping[str, str], key: str) -> str:
    return environ.get(key, "").strip()


def _read_optional_empty_text(environ: Mapping[str, str], key: str) -> str:
    value = _read_optional_text(environ, key)
    if value != "":
        raise SettingsError(f"{key} must be empty in the current example configuration")
    return value


def _read_int(
    environ: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw_value = environ.get(key, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SettingsError(f"{key} must be an integer") from exc

    if value < minimum:
        raise SettingsError(f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{key} must be <= {maximum}")
    return value


def _read_camera_tokens(raw_value: str) -> dict[str, str]:
    if raw_value.strip() == "":
        return {}

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise SettingsError("EDGE_AUTH_SECRET must be empty or a JSON object") from exc

    if not isinstance(parsed, dict):
        raise SettingsError("EDGE_AUTH_SECRET must be a JSON object")

    camera_tokens: dict[str, str] = {}
    for camera_id, token in parsed.items():
        if not isinstance(camera_id, str) or camera_id.strip() == "":
            raise SettingsError("EDGE_AUTH_SECRET camera ids must be non-empty strings")
        if not isinstance(token, str) or token == "":
            raise SettingsError("EDGE_AUTH_SECRET tokens must be non-empty strings")
        camera_tokens[camera_id] = token
    return camera_tokens
