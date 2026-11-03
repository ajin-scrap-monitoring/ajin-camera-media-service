from importlib import import_module

__all__ = [
    "AuthenticationError",
    "BearerAuthenticator",
    "ConnectionRegistry",
    "IngestProtocolError",
    "IngestService",
    "InvalidJpegFrameError",
    "InvalidTokenError",
    "LatestFrameStore",
    "MalformedAuthorizationError",
    "MissingAuthorizationError",
    "OversizedFrameError",
    "Settings",
    "SettingsError",
    "TextFrameRejectedError",
    "UnknownCameraError",
    "WebSocketServer",
    "create_app",
]


_EXPORT_MODULES = {
    name: module
    for module, names in {
        "app": ("WebSocketServer", "create_app"),
        "auth": (
            "AuthenticationError",
            "BearerAuthenticator",
            "InvalidTokenError",
            "MalformedAuthorizationError",
            "MissingAuthorizationError",
            "UnknownCameraError",
        ),
        "ingest": (
            "IngestProtocolError",
            "IngestService",
            "InvalidJpegFrameError",
            "OversizedFrameError",
            "TextFrameRejectedError",
        ),
        "registry": ("ConnectionRegistry",),
        "settings": ("Settings", "SettingsError"),
        "store": ("LatestFrameStore",),
    }.items()
    for name in names
}


def __getattr__(name):
    # Host migration/preflight tools only need standard-library storage modules.
    # Preserve the public application exports without importing its web stack.
    if name not in _EXPORT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("." + _EXPORT_MODULES[name], __name__), name)
    globals()[name] = value
    return value
