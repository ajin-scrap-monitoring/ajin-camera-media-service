from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from camera_media_service.pipeline import LiveFrameHub


class BrowserSessionAuthorizer:
    def __init__(self, valid_tickets: dict[str, str] | None = None) -> None:
        # Map ticket -> camera_id
        self._valid_tickets: dict[str, str] = valid_tickets or {}

    def register_ticket(self, ticket: str, camera_id: str) -> None:
        self._valid_tickets[ticket] = camera_id

    def authorize(self, ticket: str) -> str | None:
        return self._valid_tickets.get(ticket)


@dataclass(slots=True)
class BrowserSession:
    session_id: str
    camera_id: str
    connected_at: float


class SessionRegistry:
    def __init__(self, max_sessions: int = 100) -> None:
        self.max_sessions = max_sessions
        self._sessions: dict[str, BrowserSession] = {}

    def register(self, session: BrowserSession) -> bool:
        if len(self._sessions) >= self.max_sessions:
            return False
        self._sessions[session.session_id] = session
        return True

    def unregister(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    @property
    def active_count(self) -> int:
        return len(self._sessions)


class LiveDeliveryEndpoint:
    def __init__(
        self,
        hub: LiveFrameHub,
        authorizer: BrowserSessionAuthorizer,
        session_registry: SessionRegistry,
    ) -> None:
        self.hub = hub
        self.authorizer = authorizer
        self.session_registry = session_registry

    def create_session_id(self) -> str:
        return uuid.uuid4().hex
