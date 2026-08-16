"""Exception types shared across the integration."""

from __future__ import annotations


class CyberfoxIntoNinjaError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(CyberfoxIntoNinjaError):
    """Configuration is missing or invalid."""


class ApiError(CyberfoxIntoNinjaError):
    """An upstream API returned a response we cannot use."""

    def __init__(self, service: str, message: str, *, status: int | None = None, body: str | None = None):
        self.service = service
        self.status = status
        self.body = body
        detail = f"[{service}] {message}"
        if status is not None:
            detail += f" (HTTP {status})"
        if body:
            detail += f": {body[:500]}"
        super().__init__(detail)


class AuthError(ApiError):
    """Authentication against an upstream API failed."""
