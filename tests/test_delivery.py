from __future__ import annotations

import time

from camera_media_service.delivery import (
    BrowserSession,
    BrowserSessionAuthorizer,
    SessionRegistry,
)


def test_browser_session_authorizer_validates_tickets() -> None:
    authorizer = BrowserSessionAuthorizer({"ticket-123": "cam-1"})

    assert authorizer.authorize("ticket-123") == "cam-1"
    assert authorizer.authorize("invalid-ticket") is None

    authorizer.register_ticket("ticket-456", "cam-2")
    assert authorizer.authorize("ticket-456") == "cam-2"


def test_session_registry_enforces_max_sessions() -> None:
    registry = SessionRegistry(max_sessions=2)
    s1 = BrowserSession("s1", "cam-1", time.time())
    s2 = BrowserSession("s2", "cam-1", time.time())
    s3 = BrowserSession("s3", "cam-2", time.time())

    assert registry.register(s1) is True
    assert registry.register(s2) is True
    assert registry.register(s3) is False
    assert registry.active_count == 2

    registry.unregister("s1")
    assert registry.active_count == 1
    assert registry.register(s3) is True
