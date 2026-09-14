from __future__ import annotations

import hmac
from dataclasses import dataclass


class AuthenticationError(Exception):
    pass


class MissingAuthorizationError(AuthenticationError):
    pass


class MalformedAuthorizationError(AuthenticationError):
    pass


class InvalidTokenError(AuthenticationError):
    pass


class UnknownCameraError(AuthenticationError):
    pass


@dataclass(slots=True)
class BearerAuthenticator:
    camera_tokens: dict[str, str]

    def authenticate(self, camera_id: str, authorization: str | None) -> None:
        configured_token = self.camera_tokens.get(camera_id)
        if configured_token is None:
            raise UnknownCameraError(camera_id)

        if authorization is None or authorization == "":
            raise MissingAuthorizationError(camera_id)

        scheme, separator, token = authorization.partition(" ")
        if scheme != "Bearer" or separator != " " or token == "" or " " in token:
            raise MalformedAuthorizationError(camera_id)

        if not hmac.compare_digest(token, configured_token):
            raise InvalidTokenError(camera_id)
