"""Structured error types and Result wrapper for the SC_Toolbox ecosystem.

Provides a unified ``Result[T]`` type and exception hierarchy used by
``shared.http_client``, ``shared.cache_manager``, and individual skills.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


class SCToolboxError(Exception):
    """Base exception for all SC_Toolbox modules."""


class ApiError(SCToolboxError):
    """Raised when an API request fails."""

    def __init__(
        self,
        endpoint: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        super().__init__(f"API error for {endpoint}: {message}")


class NetworkError(ApiError):
    """Raised on network-level failures (timeout, DNS, connection)."""


class CacheError(SCToolboxError):
    """Raised when cache operations fail."""


class SchemaError(CacheError):
    """Raised when cached data fails schema validation."""


@dataclass(frozen=True)
class Result(Generic[T]):
    """Typed result wrapper carrying either data or an error."""

    data: T | None = None
    error: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        """True when the result carries valid data."""
        return self.error is None and self.data is not None

    @staticmethod
    def success(data: T) -> Result[T]:
        """Create a successful result."""
        return Result(data=data)

    @staticmethod
    def failure(error: str, error_type: str = "unknown") -> Result[T]:
        """Create a failed result."""
        return Result(error=error, error_type=error_type)
