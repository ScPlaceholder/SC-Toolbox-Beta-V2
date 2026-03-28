"""Structured error types and Result wrapper for Market Finder.

Re-exports from ``shared.errors`` for backward compatibility.
Market Finder-specific aliases (``MarketFinderError``) are kept here.
"""

from shared.errors import (  # noqa: F401
    ApiError,
    CacheError,
    NetworkError,
    Result,
    SCToolboxError,
    SchemaError,
)

# Backward-compatible alias
MarketFinderError = SCToolboxError
