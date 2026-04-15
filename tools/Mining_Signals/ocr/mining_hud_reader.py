"""Mining HUD OCR — mass and resistance extraction.

Separate from ``screen_reader.py`` on purpose:
- Different target text (rock Mass / Resistance on the mining HUD) uses
  different fonts, colors, ranges, and often *overlaps* adjacent HUD
  elements. The signal-reading pipeline is field-tuned and must not
  regress, so this lives in its own module with its own preprocessing,
  its own extractor, and its own numeric bounds.
- Reuses the Tesseract binary discovery from ``screen_reader`` so there
  is no duplicate download logic.

Exposed surface:
    scan_mining_hud(region) -> dict | None
        {'mass': float|None, 'resistance': float|None}

    extract_mass(image) -> float | None
    extract_resistance(image) -> float | None

Nothing here touches ``extract_number``, ``_preprocess_engine_a``, or
``_preprocess_engine_b`` — the signal reader is byte-for-byte unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .screen_reader import _check_tesseract, capture_region

log = logging.getLogger(__name__)

# Plausible bounds for mining HUD values. Resistance is a percentage
# (0-100). Mass on rocks ranges from ~1 up to several thousand kg — we
# use a generous upper bound to avoid false rejects on big rocks.
MIN_MASS = 0.1
MAX_MASS = 100000.0
MIN_RESISTANCE = 0.0
MAX_RESISTANCE = 100.0


# ─────────────────────────────────────────────────────────────
# Preprocessing variants tuned for the mining HUD
# ─────────────────────────────────────────────────────────────

def _preprocess_hud_variants(image) -> list:
    """Return a list of preprocessed PIL images to feed Tesseract.

    The mining HUD text is white/cyan on a semi-transparent dark panel
    and is often overlapped by progress-bar gradients (worst case for
    resistance row). Field-tested winners first, fallbacks after.
    """
    from PIL import Image, ImageOps, ImageChops, ImageFilter

    variants: list = []

    gray = image.convert("L")
    r_ch, g_ch, b_ch = image.split()

    # CRITICAL: resize BEFORE thresholding. Upscaling the grayscale
    # image first lets LANCZOS produce clean smooth edges, then the
    # threshold snaps to crisp binary. Thresholding first and then
    # upscaling smears the binary edges into mid-gray and destroys
    # Tesseract's ability to read digits — especially the leading
    # digit of a number that's already overlapped by HUD gradients.

    # ── 1. PROVEN WINNER: 5x grayscale, threshold 140 ──
    # This is the combination that survives the progress-bar overlap
    # on the RESISTANCE row in our field test. Do not remove or reorder.
    up = gray.resize((gray.width * 5, gray.height * 5), Image.LANCZOS)
    variants.append(up.point(lambda p: 255 if p > 140 else 0))

    # ── 2. 5x grayscale at alternate cutoffs (different lighting) ──
    for cutoff in (110, 170, 200):
        variants.append(up.point(lambda p, c=cutoff: 255 if p > c else 0))

    # ── 3. 4x channel-max threshold — isolates colored HUD text ──
    max_ch = ImageChops.lighter(ImageChops.lighter(r_ch, g_ch), b_ch)
    max_up = max_ch.resize((max_ch.width * 4, max_ch.height * 4), Image.LANCZOS)
    variants.append(max_up.point(lambda p: 255 if p > 140 else 0))

    # ── 4. Autocontrast + sharpen (fallback for low-contrast scenes) ──
    auto = ImageOps.autocontrast(gray, cutoff=3).filter(ImageFilter.SHARPEN)
    variants.append(auto.resize((auto.width * 4, auto.height * 4), Image.LANCZOS))

    # ── 5. Inverted (in case HUD flips to dark-on-bright) ──
    inv_up = ImageOps.invert(gray).resize((gray.width * 4, gray.height * 4), Image.LANCZOS)
    variants.append(inv_up.point(lambda p: 255 if p > 140 else 0))

    # ── 6. Blue-channel isolation (cyan HUD text has strong blue) ──
    b_up = b_ch.resize((b_ch.width * 4, b_ch.height * 4), Image.LANCZOS)
    variants.append(b_up.point(lambda p: 255 if p > 150 else 0))

    return variants


# ─────────────────────────────────────────────────────────────
# Tesseract invocation
# ─────────────────────────────────────────────────────────────

def _ocr_raw(image, config: str) -> str:
    import pytesseract
    return pytesseract.image_to_string(image, config=config).strip()


_DECIMAL_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _parse_numbers(raw: str) -> list[float]:
    """Extract all decimal numbers from raw OCR output as floats."""
    out: list[float] = []
    for m in _DECIMAL_RE.findall(raw):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:
            continue
    return out


def _vote(candidates: list[float]) -> Optional[float]:
    """Pick the best float candidate.

    Strategy (noise-resistant length preference):
    1. Require a candidate to appear at least MIN_FREQ times — filters
       out lone-wolf misreads from a single variant/PSM combination.
    2. Among candidates meeting the threshold, prefer the one with the
       most digits (partial reads like "9" when the real value is "59"
       are common when a leading digit is obscured by overlap).
    3. Break ties by frequency, then by value.
    4. Fallback: if nothing meets the threshold, drop to plain
       frequency voting on the full candidate list.
    """
    if not candidates:
        return None
    from collections import Counter

    freq = Counter(candidates)

    def digit_len(v: float) -> int:
        s = f"{v:g}".replace(".", "").replace("-", "")
        return len(s)

    # Dynamic threshold: either 3 or ~15% of all candidate reads,
    # whichever is larger. Scales with how many variants/PSMs ran.
    min_freq = max(3, len(candidates) // 7)

    filtered = [(v, c) for v, c in freq.items() if c >= min_freq]

    if filtered:
        # Longest digit string wins, then most frequent, then largest value
        filtered.sort(key=lambda kv: (digit_len(kv[0]), kv[1], kv[0]), reverse=True)
        return filtered[0][0]

    # Fallback: nothing appeared often enough — trust frequency alone
    return freq.most_common(1)[0][0]


# ─────────────────────────────────────────────────────────────
# Public extractors
# ─────────────────────────────────────────────────────────────

# PSM modes to try per variant. PSM 6/7/11 handle most HUD text; 10
# catches single characters when overlap fragments the digit group;
# 13 is raw-line without language model, useful for stylized fonts.
_PSM_MODES = ("6", "7", "10", "11", "13")


def _run_all_psms(image, char_whitelist: str) -> list[str]:
    """Run every PSM mode on a single preprocessed image, return raw texts."""
    out: list[str] = []
    for psm in _PSM_MODES:
        cfg = f"--psm {psm} -c tessedit_char_whitelist={char_whitelist}"
        try:
            out.append(_ocr_raw(image, cfg))
        except Exception:
            out.append("")
    return out


def extract_mass(image) -> Optional[float]:
    """Extract a mass value (float) from a captured HUD region."""
    if not _check_tesseract():
        return None

    try:
        variants = _preprocess_hud_variants(image)
        candidates: list[float] = []
        for v in variants:
            for raw in _run_all_psms(v, "0123456789.,"):
                for n in _parse_numbers(raw):
                    if MIN_MASS <= n <= MAX_MASS:
                        candidates.append(n)

        result = _vote(candidates)
        log.debug("mining_hud_reader: mass candidates=%s -> %s", candidates, result)
        return result
    except Exception as exc:
        log.error("mining_hud_reader: mass OCR failed: %s", exc)
        return None


def extract_resistance(image) -> Optional[float]:
    """Extract a resistance percentage (float 0-100) from a HUD region."""
    if not _check_tesseract():
        return None

    try:
        variants = _preprocess_hud_variants(image)
        candidates: list[float] = []
        for v in variants:
            for raw in _run_all_psms(v, "0123456789.,%"):
                for n in _parse_numbers(raw):
                    if MIN_RESISTANCE <= n <= MAX_RESISTANCE:
                        candidates.append(n)

        result = _vote(candidates)
        log.debug("mining_hud_reader: resistance candidates=%s -> %s", candidates, result)
        return result
    except Exception as exc:
        log.error("mining_hud_reader: resistance OCR failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────
# One-shot convenience
# ─────────────────────────────────────────────────────────────

def scan_mining_hud(
    mass_region: Optional[dict] = None,
    resistance_region: Optional[dict] = None,
) -> dict:
    """Capture + extract both fields in one call.

    Either region may be None (skipped). Returns a dict with 'mass' and
    'resistance' keys, each possibly None. Both captures run in
    parallel threads since Tesseract releases the GIL during
    ``image_to_string``.
    """
    from concurrent.futures import ThreadPoolExecutor

    result: dict = {"mass": None, "resistance": None}

    tasks = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        if mass_region is not None:
            tasks.append(("mass", pool.submit(_grab_and_extract_mass, mass_region)))
        if resistance_region is not None:
            tasks.append(("resistance", pool.submit(_grab_and_extract_resistance, resistance_region)))

        for key, fut in tasks:
            try:
                result[key] = fut.result(timeout=10)
            except Exception as exc:
                log.error("mining_hud_reader: %s task failed: %s", key, exc)

    return result


def _grab_and_extract_mass(region: dict) -> Optional[float]:
    img = capture_region(region)
    if img is None:
        return None
    return extract_mass(img)


def _grab_and_extract_resistance(region: dict) -> Optional[float]:
    img = capture_region(region)
    if img is None:
        return None
    return extract_resistance(img)
