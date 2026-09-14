from __future__ import annotations

import hmac

import pytest

from camera_media_service.auth import (
    AuthenticationError,
    BearerAuthenticator,
    InvalidTokenError,
    MalformedAuthorizationError,
    MissingAuthorizationError,
    UnknownCameraError,
)


def test_bearer_authenticator_accepts_exact_bearer_token_for_known_camera() -> None:
    authenticator = BearerAuthenticator({"cam-1": "token-1"})

    authenticator.authenticate("cam-1", "Bearer token-1")


def test_bearer_authenticator_uses_constant_time_comparison_after_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticator = BearerAuthenticator({"cam-1": "token-1"})
    monkeypatch.setattr(hmac, "compare_digest", lambda left, right: False)

    with pytest.raises(InvalidTokenError):
        authenticator.authenticate("cam-1", "Bearer token-1")


@pytest.mark.parametrize(
    ("camera_id", "authorization", "error_type"),
    [
        ("cam-1", None, MissingAuthorizationError),
        ("cam-1", "", MissingAuthorizationError),
        ("cam-1", "Bearer", MalformedAuthorizationError),
        ("cam-1", "bearer token-1", MalformedAuthorizationError),
        ("cam-1", "Basic token-1", MalformedAuthorizationError),
        ("cam-1", "Bearer wrong-token", InvalidTokenError),
        ("cam-2", "Bearer token-1", UnknownCameraError),
    ],
)
def test_bearer_authenticator_rejects_missing_malformed_wrong_and_unknown_credentials(
    camera_id: str,
    authorization: str | None,
    error_type: type[AuthenticationError],
) -> None:
    authenticator = BearerAuthenticator({"cam-1": "token-1"})

    with pytest.raises(error_type):
        authenticator.authenticate(camera_id, authorization)
