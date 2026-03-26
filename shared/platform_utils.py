"""
Windows-specific platform utilities shared across tools.
"""
import ctypes
import logging
import zlib

log = logging.getLogger(__name__)


def set_dpi_awareness() -> None:
    """Set per-monitor DPI awareness, preferring V2.  No-op on non-Windows or failure."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # V1
        except Exception:
            pass


def deterministic_hotkey_id(hotkey_str: str) -> int:
    """Return a deterministic int ID for RegisterHotKey from a hotkey string.

    Uses CRC32 to map the string to the valid range [1, 0xBFFF].
    Used by mining_loadout_app.py for Win32 RegisterHotKey/UnregisterHotKey.
    """
    return (zlib.crc32(hotkey_str.encode()) & 0xBFFF) + 1

