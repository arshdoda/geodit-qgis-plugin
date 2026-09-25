"""Typed API errors.

The server's error bodies are not uniform: business errors are
``{message, errors?}``, framework 401/429 are ``{detail}``. Callers branch on
the exception class (status), never on message text.
"""

from __future__ import annotations

from typing import Dict, List, Optional


class ApiError(Exception):
    status: int = 0

    def __init__(
        self,
        message: str = "",
        *,
        status: Optional[int] = None,
        errors: Optional[Dict[str, List[str]]] = None,
        retry_after: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.default_message()
        if status is not None:
            self.status = status
        self.errors = errors or {}
        self.retry_after = retry_after
        self.request_id = request_id

    def default_message(self) -> str:
        return f"Server error ({self.status})"

    def field_message(self, field: str) -> Optional[str]:
        msgs = self.errors.get(field)
        return msgs[0] if msgs else None


class NetworkError(ApiError):
    """No HTTP response at all (DNS, TLS, timeout, offline)."""

    def default_message(self) -> str:
        return "Could not reach the Geodit server."


class BadRequest(ApiError):
    status = 400


class Unauthorized(ApiError):
    status = 401

    def default_message(self) -> str:
        return "Not signed in."


class SessionExpired(Unauthorized):
    """The refresh token is dead (expired, revoked, logged out elsewhere).
    The user must sign in again — a stored password is never replayed."""

    def default_message(self) -> str:
        return "Your session has expired. Please sign in again."


class PlanExpired(ApiError):
    status = 402

    def default_message(self) -> str:
        return "The project owner's plan has expired. Changes can't be uploaded until it is renewed."


class PermissionDenied(ApiError):
    status = 403

    def default_message(self) -> str:
        return "You don't have access to this."


class NotFound(ApiError):
    status = 404

    def default_message(self) -> str:
        return "Not found."


class Conflict(ApiError):
    status = 409


class ValidationFailed(ApiError):
    status = 422

    def default_message(self) -> str:
        return "Validation failed."


class RateLimited(ApiError):
    status = 429

    def default_message(self) -> str:
        return "Too many requests. Please try again later."


class ServerError(ApiError):
    status = 500


class ServerTooOld(ApiError):
    """The server predates this plugin version: no ``projects/desktop/list``
    (404), or a ``user/refresh`` that still wants the token in the URL (422)."""

    def default_message(self) -> str:
        return (
            "This Geodit server doesn't support this version of the plugin yet. "
            "Ask your Geodit administrator, or choose another server."
        )


_BY_STATUS = {
    400: BadRequest,
    401: Unauthorized,
    402: PlanExpired,
    403: PermissionDenied,
    404: NotFound,
    409: Conflict,
    422: ValidationFailed,
    429: RateLimited,
}


def error_for_status(status: int):
    if status >= 500:
        return ServerError
    return _BY_STATUS.get(status, ApiError)
