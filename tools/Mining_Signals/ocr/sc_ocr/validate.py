"""Per-domain format / range / dictionary validation.

Post-classification layer that filters out segmentation errors and
OCR hallucinations before they reach the UI. Each domain has its
own set of validators; failed reads fall through to ONNX fallback
in ``api.py`` before being returned as None.

Refinery validators reuse the existing fuzzy-match infrastructure
in ``ocr/refinery_reader.py`` — no reinvention.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger(__name__)


# ── Signal scanner ─────────────────────────────────────────────────

SIGNAL_MIN = 1000
SIGNAL_MAX = 35000


def validate_signal(raw: str) -> Optional[int]:
    """Parse a digit-only string as a signal number in [1000, 35000]."""
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        return None
    try:
        val = int(digits)
    except ValueError:
        return None
    if SIGNAL_MIN <= val <= SIGNAL_MAX:
        return val
    # Strip leading digit icons (HUD sometimes has a decorative
    # digit-like icon glued to the value). Try dropping one leading
    # char.
    if len(digits) >= 4:
        try:
            val2 = int(digits[1:])
            if SIGNAL_MIN <= val2 <= SIGNAL_MAX:
                return val2
        except ValueError:
            pass
    return None


# ── Mining HUD ─────────────────────────────────────────────────────

MASS_MAX = 10_000_000.0  # kg — large asteroids can exceed a million


def validate_mass(raw: str) -> Optional[float]:
    """Parse a mass read as a float in [0.1, MASS_MAX]."""
    cleaned = re.sub(r"[^0-9.]", "", raw)
    if not cleaned:
        return None
    # Collapse accidental double dots
    cleaned = re.sub(r"\.+", ".", cleaned).strip(".")
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if 0.1 <= val <= MASS_MAX:
        return val
    return None


def validate_pct(raw: str) -> Optional[float]:
    """Parse a percentage read as a float in [0, 100]."""
    # Strip trailing % and inner whitespace; keep digits + dot
    cleaned = re.sub(r"[^0-9.]", "", raw.replace("%", ""))
    if not cleaned:
        return None
    cleaned = re.sub(r"\.+", ".", cleaned).strip(".")
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if 0.0 <= val <= 100.0:
        return val
    # Sometimes trailing digit is a misread '%' — try dropping
    # progressively from the right.
    for end in range(len(cleaned) - 1, 0, -1):
        try:
            v = float(cleaned[:end])
            if 0.0 <= v <= 100.0:
                return v
        except ValueError:
            continue
    return None


def validate_instability(
    raw: str,
    confidences: list[float] | None = None,
) -> Optional[float]:
    """Parse an instability read as a float in [0, 100000].

    Wider bounds than pct because instability can reach 4-digit
    values on some ores.

    Special handling: the ONNX model sometimes classifies the '.'
    glyph as '8' (similar circular shape, low confidence). If the
    raw string has no dot AND exceeds a plausible instability value,
    try inserting a dot at the lowest-confidence position to recover
    the decimal.
    """
    cleaned = re.sub(r"[^0-9.]", "", raw)
    if not cleaned:
        return None
    cleaned = re.sub(r"\.+", ".", cleaned).strip(".")
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if 0.0 <= val <= 100000.0:
        return val

    # Decimal recovery: if no dot found and value is too large,
    # try inserting a dot at the lowest-confidence position.
    if "." not in cleaned and confidences and len(confidences) == len(raw):
        # Find the lowest-confidence character
        min_idx = int(min(range(len(confidences)), key=lambda i: confidences[i]))
        # Try replacing that character with a dot
        attempt = raw[:min_idx] + "." + raw[min_idx + 1:]
        attempt_clean = re.sub(r"[^0-9.]", "", attempt)
        attempt_clean = re.sub(r"\.+", ".", attempt_clean).strip(".")
        try:
            val2 = float(attempt_clean)
            if 0.0 <= val2 <= 100000.0:
                return val2
        except ValueError:
            pass

    return None


# ── Refinery ───────────────────────────────────────────────────────
# Delegated to ocr/refinery_reader.py's existing fuzzy matchers.
# They operate on the raw OCR text (after glyph joins) and return
# the canonical form. We re-import lazily to avoid a hard circular
# dependency.

def validate_refinery_method(raw: str) -> Optional[str]:
    try:
        from .. import refinery_reader
    except Exception:
        return raw.strip() or None
    # refinery_reader has _fuzzy_method which does the matching.
    matcher = getattr(refinery_reader, "_fuzzy_method", None)
    if matcher is None:
        return raw.strip() or None
    try:
        return matcher(raw) or None
    except Exception:
        return raw.strip() or None


def validate_refinery_commodity(raw: str) -> Optional[str]:
    try:
        from .. import refinery_reader
    except Exception:
        return raw.strip() or None
    matcher = getattr(refinery_reader, "_fuzzy_mineral", None)
    if matcher is None:
        return raw.strip() or None
    try:
        return matcher(raw) or None
    except Exception:
        return raw.strip() or None


def validate_refinery_time(raw: str) -> Optional[int]:
    try:
        from .. import refinery_reader
    except Exception:
        return None
    parser = getattr(refinery_reader, "_parse_time_to_seconds", None)
    if parser is None:
        return None
    try:
        secs = parser(raw)
        if secs and secs > 0:
            return int(secs)
    except Exception:
        pass
    return None


def validate_refinery_cost(raw: str) -> Optional[float]:
    # Allow digits, commas, dots, keep the first number-looking thing
    match = re.search(r"[\d,]+(?:\.\d{1,2})?", raw)
    if not match:
        return None
    s = match.group(0).replace(",", "")
    try:
        val = float(s)
    except ValueError:
        return None
    if 1.0 <= val <= 1_000_000_000.0:
        return val
    return None
