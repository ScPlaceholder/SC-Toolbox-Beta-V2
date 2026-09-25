"""
SuitMk2 - Thread-safe State Store

Provides a thread-safe key-value store for tracking game state
with change notification support.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class StateStore:
    """Thread-safe key-value store with change notifications."""

    def __init__(self) -> None:
        self._state: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[str, Any, Any], None]] = []

    def set(self, key: str, value: Any) -> bool:
        """Set a state value. Returns True if value changed."""
        with self._lock:
            old_value = self._state.get(key)
            if old_value == value:
                return False
            self._state[key] = value

        # Snapshot to avoid mutation during iteration
        with self._lock:
            subs = list(self._subscribers)
        for callback in subs:
            try:
                callback(key, old_value, value)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Error in state subscriber for key '%s'", key
                )

        return True

    def get(self, key: str, default: Any = None) -> Any:
        """Get a state value."""
        with self._lock:
            return self._state.get(key, default)

    def get_all(self) -> dict[str, Any]:
        """Get a copy of all states."""
        with self._lock:
            return self._state.copy()

    def subscribe(self, callback: Callable[[str, Any, Any], None]) -> None:
        """Subscribe to state changes. Callback: (key, old_value, new_value)."""
        with self._lock:
            self._subscribers = [*self._subscribers, callback]

    def clear(self) -> None:
        """Clear all states."""
        with self._lock:
            self._state.clear()

    def clear_key(self, key: str) -> None:
        """Clear a specific key (set to None, fires notification)."""
        self.set(key, None)

    def load_dict(self, data: dict[str, Any]) -> None:
        """Bulk-load state without triggering change notifications."""
        with self._lock:
            self._state.update(data)

    def keys_matching(self, prefix: str) -> dict[str, Any]:
        """Return all keys matching a prefix with their values."""
        with self._lock:
            return {
                k: v for k, v in self._state.items()
                if k.startswith(prefix) and v is not None
            }
