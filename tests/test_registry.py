from __future__ import annotations

from camera_media_service.registry import ConnectionRegistry


def test_connection_registry_claim_replaces_old_connection_and_returns_displaced_id() -> None:
    registry = ConnectionRegistry()

    assert registry.claim("cam-1", "conn-1") is None
    assert registry.claim("cam-1", "conn-2") == "conn-1"


def test_connection_registry_release_ignores_stale_connection() -> None:
    registry = ConnectionRegistry()

    registry.claim("cam-1", "conn-1")
    registry.claim("cam-1", "conn-2")

    assert registry.release("cam-1", "conn-1") is False
    assert registry.release("cam-1", "conn-2") is True
