"""
Mining Signals — Refinery Panel OCR (Dual-Engine)

Detects the Star Citizen refinery kiosk on screen and extracts work order
details using a dual-engine approach:
  Engine A: Tesseract (full-text, multi-variant preprocessing)
  Engine B: ONNX CNN (character-level, for numeric fields)

Cross-validates numeric values between engines. Text fields (commodity
names, method) use Tesseract with fuzzy matching against known lists.
"""

from __future__ import annotations

import logging
import re
from difflib import get_close_matches
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known Star Citizen refinery data
# ---------------------------------------------------------------------------

KNOWN_MINERALS: list[str] = [
    # High-value ores
    "Quantanium", "Bexalite", "Taranite", "Agricium", "Laranite",
    # Standard ores
    "Beryl", "Borase", "Hephaestanite", "Titanium", "Gold",
    "Diamond", "Copper", "Aluminium", "Aluminum", "Corundum", "Tungsten",
    "Iron", "Quartz", "Cobalt", "Silicon", "Tin",
    # Refined/processed
    "Stannite", "Stilphane", "Stileron",
    # Hand-minable gems
    "Aphorite", "Dolivine", "Hadanite", "Janalite",
    "Prota", "Riccite",
    # Ice
    "Raw Ice",
    # Misc ores
    "Ammonia", "Aslarite", "Beradom", "Feynmaline", "Glacosite",
    "Jaclium", "Lindinium", "Savrilium", "Torite",
    # Inert (filtered out in parsing but needed for fuzzy match)
    "Inert Material", "Inert Materials",
]

KNOWN_METHODS: list[str] = [
    "Cormack",
    "Cormack Method",
    "Dinyx Solventation",
    "Electrostarolysis",
    "Ferron Exchange",
    "Gaskin Process",
    "Kazen Winnowing",
    "Pyrometric Chromalysis",
    "Thermonatic Deposition",
    "XCR Reaction",
]

# Anchor keywords that indicate the refinery kiosk is on screen
_ANCHOR_KEYWORDS = [
    "work order", "refinery", "processing", "cost",
    "raw materials", "get quote", "cancel",
    "manifest", "refine", "yield", "total cost",
    "materials selected", "quality", "setup", "completed",
    "corundum", "aluminum", "beryl", "quantanium", "bexalite",
    "taranite", "agricium", "laranite", "borase", "hephaestanite",
    "select an option", "storage option",
]

# ---------------------------------------------------------------------------
# Preprocessing variants
# ---------------------------------------------------------------------------


def _preprocess_variants(img) -> list:
    """Generate multiple preprocessed versions for Tesseract.

    The refinery kiosk uses white/cyan text on a dark semi-transparent panel.
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    import numpy as np

    results = []
    w, h = img.size
    scale = 4

    # Convert to numpy for channel ops
    arr = np.array(img)

    # Variant 1: Grayscale, 4x upscale, threshold 120
    gray = img.convert("L")
    up = gray.resize((w * scale, h * scale), Image.LANCZOS)
    bw = up.point(lambda p: 255 if p > 120 else 0)
    results.append(bw)

    # Variant 2: Grayscale threshold 150
    bw2 = up.point(lambda p: 255 if p > 150 else 0)
    results.append(bw2)

    # Variant 3: Grayscale threshold 180
    bw3 = up.point(lambda p: 255 if p > 180 else 0)
    results.append(bw3)

    # Variant 4: Blue channel isolation (kiosk headers are cyan/blue)
    blue = Image.fromarray(arr[:, :, 2])
    blue_up = blue.resize((w * scale, h * scale), Image.LANCZOS)
    blue_bw = blue_up.point(lambda p: 255 if p > 130 else 0)
    results.append(blue_bw)

    # Variant 5: Autocontrast + sharpen
    auto = ImageOps.autocontrast(gray)
    auto_up = auto.resize((w * scale, h * scale), Image.LANCZOS)
    auto_sharp = auto_up.filter(ImageFilter.SHARPEN)
    auto_bw = auto_sharp.point(lambda p: 255 if p > 140 else 0)
    results.append(auto_bw)

    # Variant 6: Inverted (for edge cases with bright backgrounds)
    inv = ImageOps.invert(gray)
    inv_up = inv.resize((w * scale, h * scale), Image.LANCZOS)
    inv_bw = inv_up.point(lambda p: 255 if p > 140 else 0)
    results.append(inv_bw)

    return results


# ---------------------------------------------------------------------------
# Engine A: Tesseract full-text OCR
# ---------------------------------------------------------------------------


def _tesseract_full_text(img, psm: str = "6") -> str:
    """Run Tesseract with full alphanumeric whitelist on a preprocessed image."""
    try:
        import pytesseract
        config = (
            f"--psm {psm} "
            "-c tessedit_char_whitelist="
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            "0123456789.,:%/() -"
        )
        return pytesseract.image_to_string(img, config=config).strip()
    except Exception as exc:
        log.debug("Tesseract call failed: %s", exc)
        return ""


def _tesseract_digits_only(img, psm: str = "7") -> str:
    """Run Tesseract with digits-only whitelist."""
    try:
        import pytesseract
        config = f"--psm {psm} -c tessedit_char_whitelist=0123456789.,%:"
        return pytesseract.image_to_string(img, config=config).strip()
    except Exception as exc:
        log.debug("Tesseract digits call failed: %s", exc)
        return ""


def _engine_a_full_text(img) -> list[str]:
    """Engine A (fallback): run Tesseract on preprocessing variants.

    Only used when PaddleOCR is unavailable.
    """
    variants = _preprocess_variants(img)
    results = []
    for variant in variants:
        for psm in ("6", "11"):
            text = _tesseract_full_text(variant, psm)
            if text:
                results.append(text)
        text_raw = _tesseract_raw(variant, psm="6")
        if text_raw:
            results.append(text_raw)
    return results


# ---------------------------------------------------------------------------
# Engine P: PaddleOCR (preferred — structured text with positions)
# ---------------------------------------------------------------------------


def _engine_paddle(img) -> list[dict] | None:
    """Run PaddleOCR on the image. Returns list of text regions or None.

    Each region: {"text": str, "conf": float, "y_mid": int}
    Returns None if Paddle is unavailable (caller falls back to Tesseract).
    """
    try:
        from ocr.paddle_client import is_available, recognize
        if not is_available():
            return None
        result = recognize(img)
        return result
    except ImportError:
        return None
    except Exception as exc:
        log.debug("PaddleOCR failed: %s", exc)
        return None


_SKIP_TEXTS = {
    "quality", "qty", "yield", "refine", "materials selected",
    "aterials selected", "elected", "processing", "confirm", "cancel",
    "setup", "completed", "work order", "total", "cost",
    "online", "high", "low", "moderate", "speed",
    "order", "in process", "in proce", "time rema", "details",
    "submitted", "submitt", "expected", "expecte", "rename", "delete",
    "pin", "unpin", "commodities", "method", "material",
    "rder", "rocessing", "gsing time",
}

# Text patterns that mark work order boundaries
_ORDER_BOUNDARY_RE = re.compile(
    r"WORK\s*ORDER|SETUP|COMPLETED|RAW\s*MATERIALS",
    re.IGNORECASE,
)


def _filter_popup_regions(regions: list[dict]) -> list[dict]:
    """Remove text regions from overlapping popup windows using x-position."""
    max_x = 0
    for r in regions:
        if r.get("text", "").upper() in ("YIELD", "REFINE"):
            max_x = max(max_x, r.get("x_mid", 0))
    if max_x > 0:
        x_cutoff = int(max_x * 1.3)
        return [r for r in regions if r.get("x_mid", 0) <= x_cutoff]
    return regions


def _regions_to_rows(regions: list[dict]) -> list[list[dict]]:
    """Group text regions into rows by y_mid proximity (20px threshold)."""
    sorted_regions = sorted(regions, key=lambda r: r.get("y_mid", 0))
    rows: list[list[dict]] = []
    current_row: list[dict] = []
    last_y = -100
    for r in sorted_regions:
        y = r.get("y_mid", 0)
        if abs(y - last_y) > 20 and current_row:
            rows.append(current_row)
            current_row = []
        current_row.append(r)
        last_y = y
    if current_row:
        rows.append(current_row)
    return rows


def _parse_single_order_section(regions: list[dict]) -> dict | None:
    """Parse a single work order's worth of Paddle regions.

    Returns dict with commodities, method, cost, processing_seconds, station
    or None if nothing useful extracted.
    """
    rows = _regions_to_rows(regions)

    commodities = []
    method = ""
    cost = 0.0
    processing_seconds = 0

    # ── Extract method ──
    for r in regions:
        m = _fuzzy_method(r["text"])
        if m:
            method = m
            break

    # ── Extract cost ──
    for r in regions:
        cost_match = re.search(r"(\d[\d,]*\.\d{2})", r["text"])
        if cost_match:
            try:
                val = float(cost_match.group(1).replace(",", ""))
                if val > 5:
                    cost = val
                    break
            except ValueError:
                pass

    # ── Extract processing time ──
    time_parts = []
    for r in regions:
        text = r["text"].strip()
        if re.match(r"^\d+[hHmMsS]$", text):
            time_parts.append(text)
        elif re.match(r"^\d+[hH]\s*\d+[mM]", text):
            time_parts.append(text)
        elif re.match(r"^\d+[mM]\s*\d+[sS]", text):
            time_parts.append(text)
    if time_parts:
        time_str = " ".join(time_parts)
        processing_seconds = _parse_time_to_seconds(time_str)
    if processing_seconds == 0:
        for row in rows:
            row_line = " ".join(r["text"] for r in row)
            t = _parse_time_to_seconds(row_line)
            if t > 0:
                processing_seconds = t
                break

    # ── Extract commodities ──
    for row in rows:
        sorted_row = sorted(row, key=lambda r: r.get("x_mid", 0))

        mineral = None
        for text in [r["text"] for r in sorted_row]:
            text_lower = text.strip().lower()
            if text_lower in _SKIP_TEXTS:
                continue
            if len(text.strip()) < 3:
                continue
            m = _fuzzy_mineral(text)
            if m and "inert" not in m.lower():
                mineral = m
                break

        if not mineral:
            continue

        nums = []
        for r in sorted_row:
            text = r["text"].strip()
            if re.match(r"^\d+$", text):
                nums.append(int(text))

        quality = nums[0] if len(nums) >= 1 else 0
        qty = nums[1] if len(nums) >= 2 else 0
        yld = nums[2] if len(nums) >= 3 else 0

        commodities.append({
            "name": mineral,
            "quality": quality,
            "qty": qty,
            "scu": yld,
        })

    # Filter out commodities with all-zero values — these are false
    # positives where the OCR matched a mineral name in non-refinery
    # UI text but found no actual quantities.
    commodities = [
        c for c in commodities
        if c.get("qty", 0) > 0 or c.get("scu", 0) > 0
    ]

    has_minerals = len(commodities) > 0
    has_details = bool(method) or cost > 0 or processing_seconds > 0
    if not has_minerals or not has_details:
        return None

    return {
        "commodities": commodities,
        "method": method,
        "cost": cost,
        "processing_seconds": processing_seconds,
        "station": "",
    }


def _parse_paddle_results(regions: list[dict]) -> list[dict]:
    """Parse PaddleOCR text regions into one or more order dicts.

    Detects work order boundaries (WORK ORDER, SETUP, RAW MATERIALS)
    and splits regions into sections, parsing each independently.
    Returns a list of order dicts (may be empty).
    """
    if not regions:
        return []

    # Filter out popup overlay regions
    regions = _filter_popup_regions(regions)

    # Sort by y_mid
    sorted_regions = sorted(regions, key=lambda r: r.get("y_mid", 0))

    # Find boundary indices where a new order section starts.
    # Look for "WORK ORDER", "SETUP", "RAW MATERIALS" text as delimiters.
    boundary_indices: list[int] = []
    for i, r in enumerate(sorted_regions):
        text_upper = r.get("text", "").upper().strip()
        if "WORK ORDER" in text_upper or text_upper == "SETUP":
            boundary_indices.append(i)

    # If no boundaries found, treat everything as one order
    if not boundary_indices:
        order = _parse_single_order_section(sorted_regions)
        return [order] if order else []

    # Split regions into sections between boundaries
    # Each section = one potential work order
    sections: list[list[dict]] = []
    for idx, start in enumerate(boundary_indices):
        end = boundary_indices[idx + 1] if idx + 1 < len(boundary_indices) else len(sorted_regions)
        section = sorted_regions[start:end]
        if section:
            sections.append(section)

    # Parse each section independently
    orders = []
    for section in sections:
        order = _parse_single_order_section(section)
        if order and (order["commodities"] or order["method"]):
            orders.append(order)

    # If no orders from sections, try the whole thing as one
    if not orders:
        order = _parse_single_order_section(sorted_regions)
        if order:
            orders.append(order)

    return orders


# ---------------------------------------------------------------------------
# Parsing extracted text into structured order data
# ---------------------------------------------------------------------------

_RE_SCU = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:cSCU|SCU|cscu|scu)", re.IGNORECASE)
_RE_QTY = re.compile(r"(\d+)")  # plain integer quantity from column
_RE_COST = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)\s*(?:aUEC|UEC|uec)", re.IGNORECASE)
_RE_COST_PLAIN = re.compile(r"(?:TOTAL\s*COST|COST)\s*[:\s]*(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)
# Matches lines like "CORUNDUM (RAW) 597 63 20" or "RAW ICE 255 1829 --"
# Also handles OCR garbage after yield (e.g. "493C", "289 ||")
_RE_MATERIAL_LINE = re.compile(
    r"([A-Za-z][A-Za-z\s]{2,}?)"  # Material name (2+ letter chars)
    r"\s*(?:\([A-Z]+\))?\s*"      # Optional (RAW) / (ORE) suffix
    r"(\d+)\s+(\d+)"              # Quality, Qty columns (required)
    r"(?:\s+(\d+))?",             # Yield column (optional — may be "--")
    re.IGNORECASE,
)
_RE_TIME = re.compile(
    r"(?:(\d+)\s*[hH](?:ours?|r)?\.?\s*)?"
    r"(?:(\d+)\s*[mM](?:in(?:utes?)?)?\.?\s*)?"
    r"(?:(\d+)\s*[sS](?:ec(?:onds?)?)?)?"
)
_RE_TIME_COLON = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
# Matches "19m 3s", "0m 47s", "1h 30m", "19m 35" (trailing number = seconds)
_RE_TIME_NATURAL = re.compile(
    r"(?:(\d+)\s*h\w*\s*)?"
    r"(\d+)\s*m\w*"
    r"(?:\s*(\d+)\s*s?\w*)?",
    re.IGNORECASE,
)


_MINERAL_ALIASES: dict[str, str] = {
    "WICE": "Raw Ice",
    "W ICE": "Raw Ice",
    "RAW ICE": "Raw Ice",
    "RAWICE": "Raw Ice",
    "CORUNDUM": "Corundum",
    "ALUMINIUM": "Aluminium",
    "ALUMINUM": "Aluminum",
}


def _fuzzy_mineral(text: str) -> str | None:
    """Match text to a known mineral name. Strips (RAW)/(ORE) suffixes."""
    text_clean = re.sub(r"\s*\([A-Z]+\)\s*$", "", text.strip(), flags=re.IGNORECASE).strip()
    if not text_clean or len(text_clean) < 3:
        return None
    # Check aliases first (handles OCR truncations like "WICE")
    upper = text_clean.upper()
    if upper in _MINERAL_ALIASES:
        return _MINERAL_ALIASES[upper]
    # Also try title-case version for better matching
    candidates = [text_clean, text_clean.title(), text_clean.capitalize()]
    for candidate in candidates:
        matches = get_close_matches(
            candidate, KNOWN_MINERALS, n=1, cutoff=0.6
        )
        if matches:
            return matches[0]
    return None


def _fuzzy_method(text: str) -> str | None:
    """Match text to a known refining method.

    Checks both exact fuzzy match on the full line and substring containment.
    """
    text_clean = text.strip()
    if not text_clean or len(text_clean) < 4:
        return None
    # Direct fuzzy match
    matches = get_close_matches(
        text_clean, KNOWN_METHODS, n=1, cutoff=0.55
    )
    if matches:
        return matches[0]
    # Substring check: does any known method appear inside the text?
    text_lower = text_clean.lower()
    for method in KNOWN_METHODS:
        if method.lower() in text_lower:
            return method
    # Partial word match: check if any word matches a method's last word
    # (handles Paddle reading "WINNOWING" without "KAZEN")
    words = text_lower.split()
    for word in words:
        if len(word) < 4:
            continue
        for method in KNOWN_METHODS:
            method_words = method.lower().split()
            for mw in method_words:
                if mw.startswith(word) or word.startswith(mw):
                    if len(word) >= 5 or len(mw) >= 5:
                        return method
    return None


def _parse_time_to_seconds(text: str) -> int:
    """Parse a time string like '1h 30m', '19m 3s', '0m 47s', '01:30:00'."""
    # Try colon format first: "01:30:00" or "30:00"
    m = _RE_TIME_COLON.search(text)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total

    # Try natural format with the better regex: "19m 3s", "19m 35", "1h 30m"
    m = _RE_TIME_NATURAL.search(text)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total

    # Fallback: original pattern
    m = _RE_TIME.search(text)
    if m and any(m.group(i) for i in (1, 2, 3)):
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total

    return 0


def _parse_cost(text: str) -> float:
    """Extract cost value from text."""
    m = _RE_COST.search(text)
    if m:
        val = m.group(1).replace(",", "")
        try:
            return float(val)
        except ValueError:
            pass
    return 0.0


def _parse_ocr_results(
    tesseract_texts: list[str],
    onnx_numbers: list[str],
) -> list[dict]:
    """Parse raw OCR outputs into structured order data.

    The refinery SETUP panel has this layout:
      CORUNDUM (RAW)  597  63  20
      ALUMINUM (ORE)  443  99  31
      INERT MATERIALS  0  116  0
      XCR Reaction  (method dropdown)
      TOTAL COST  364.00 aUEC
      PROCESSING TIME  0m 47s

    Returns list of order dicts with keys:
      commodities, method, cost, processing_seconds, station
    """
    from collections import Counter

    all_text = "\n".join(tesseract_texts)
    all_lines = all_text.split("\n")

    # ── Extract commodities ──
    # Layout: NAME (TYPE) QUALITY QTY YIELD
    # e.g.  "CORUNDUM (RAW) 597 63 24"
    #
    # Parse each variant independently, collect all candidate entries,
    # then vote: group by (mineral_name, position_index) and pick the
    # most common quality/qty/yield reading for each position.

    # Step 1: collect all raw material reads with their line position
    raw_reads: list[dict] = []  # {name, quality, qty, scu, position}
    material_position = 0

    for line in all_lines:
        line = line.strip()
        if not line:
            continue

        m = _RE_MATERIAL_LINE.search(line)
        if m:
            raw_name = m.group(1).strip()
            quality = int(m.group(2))
            qty = int(m.group(3))
            yld = int(m.group(4)) if m.group(4) else 0  # yield may be "--"
            mineral = _fuzzy_mineral(raw_name)
            if mineral:
                raw_reads.append({
                    "name": mineral,
                    "quality": quality,
                    "qty": qty,
                    "scu": yld,
                })

    # Step 2: Filter out inert materials
    raw_reads = [r for r in raw_reads if "inert" not in r["name"].lower()]

    # Step 3: Vote — group by (name, quality) and keep entries that
    # appear most often. OCR variants that read "5897" instead of "597"
    # are outvoted by the majority that read "597".
    from collections import Counter

    # Count how many times each exact (name, quality, qty, scu) tuple appears
    entry_counts: Counter = Counter()
    for r in raw_reads:
        key = (r["name"], r["quality"], r["qty"], r["scu"])
        entry_counts[key] += 1

    # Group by (name, approximate_quality) to merge OCR variants of same entry
    # Use quality buckets: values within 20% are the same entry
    commodities = []
    used_keys: set = set()

    for (name, quality, qty, scu), count in entry_counts.most_common():
        # Check if we already have an entry for this mineral at similar quality
        already = False
        for existing in commodities:
            if existing["name"] == name:
                eq = existing["quality"]
                if eq == 0 or quality == 0:
                    if eq == quality:
                        already = True
                        break
                elif abs(eq - quality) / max(eq, quality) < 0.05:
                    already = True
                    break
        if already:
            continue

        commodities.append({
            "name": name,
            "quality": quality,
            "qty": qty,
            "scu": scu,
        })
        used_keys.add((name, quality))

    # ── Extract method ──
    method = ""
    for line in all_lines:
        m = _fuzzy_method(line.strip())
        if m:
            method = m
            break

    # ── Extract cost ──
    costs = []
    _re_decimal = re.compile(r"(\d[\d,]*\.\d{2})")  # matches "442.00", "2657.00"
    for text in tesseract_texts:
        # Try "364.00 aUEC" format
        c = _parse_cost(text)
        if c > 0:
            costs.append(c)
        # Try "TOTAL COST 364.00" format (without aUEC label)
        m = _RE_COST_PLAIN.search(text)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    costs.append(val)
            except ValueError:
                pass
        # Fallback: any X.XX decimal near "cost" or standalone
        for line in text.split("\n"):
            line_lower = line.strip().lower()
            if "cost" in line_lower or "auec" in line_lower:
                for dm in _re_decimal.finditer(line):
                    try:
                        val = float(dm.group(1).replace(",", ""))
                        if 0 < val < 1_000_000:
                            costs.append(val)
                    except ValueError:
                        pass
    # ONNX numbers
    for num_str in onnx_numbers:
        clean = num_str.replace(",", "").replace("%", "")
        try:
            val = float(clean)
            if 1 < val < 1_000_000:
                costs.append(val)
        except ValueError:
            pass
    cost = 0.0
    if costs:
        rounded = [round(c) for c in costs]
        most_common = Counter(rounded).most_common(1)
        if most_common:
            cost = float(most_common[0][0])

    # ── Extract processing time ──
    times = []
    for text in tesseract_texts:
        for line in text.split("\n"):
            line_s = line.strip()
            if not line_s:
                continue
            t = _parse_time_to_seconds(line_s)
            if t > 0:
                times.append(t)
    processing_seconds = 0
    if times:
        most_common = Counter(times).most_common(1)
        if most_common:
            processing_seconds = most_common[0][0]

    # Inert materials already filtered in Step 2 above.
    # Filter out commodities with all-zero values — prevents false
    # positives where the OCR matched a mineral name in non-refinery
    # UI text but found no actual quantities.
    commodities = [
        c for c in commodities
        if c.get("qty", 0) > 0 or c.get("scu", 0) > 0
    ]

    # Require at least one real mineral AND (a method OR cost OR time)
    has_minerals = len(commodities) > 0
    has_details = bool(method) or cost > 0 or processing_seconds > 0
    if not has_minerals or not has_details:
        return []

    return [{
        "commodities": commodities,
        "method": method,
        "cost": cost,
        "processing_seconds": processing_seconds,
        "station": "",
    }]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_refinery_panel(region: dict) -> bool:
    """Fast check: is the refinery kiosk visible in the given screen region?

    Tries PaddleOCR first (single call, fast). Falls back to Tesseract
    multi-variant if Paddle unavailable.
    Returns True if ≥2 anchor keywords found.
    """
    from ocr.screen_reader import capture_region, is_ocr_available

    if not is_ocr_available():
        return False

    img = capture_region(region)
    if img is None:
        return False

    # Try Paddle first — single fast call
    paddle_regions = _engine_paddle(img)
    if paddle_regions is not None:
        all_text = " ".join(r.get("text", "") for r in paddle_regions).lower()
        hits = sum(1 for kw in _ANCHOR_KEYWORDS if kw in all_text)
        return hits >= 2

    # Fallback: Tesseract multi-variant
    from PIL import ImageOps

    gray = img.convert("L")
    w, h = gray.size
    all_text = ""

    up = gray.resize((w * 4, h * 4))
    bw = up.point(lambda p: 255 if p > 100 else 0)
    all_text += " " + _tesseract_raw(bw, psm="6")

    bw2 = up.point(lambda p: 255 if p > 140 else 0)
    all_text += " " + _tesseract_raw(bw2, psm="6")

    blue = img.split()[2]
    blue_up = blue.resize((w * 4, h * 4))
    blue_bw = blue_up.point(lambda p: 255 if p > 100 else 0)
    all_text += " " + _tesseract_raw(blue_bw, psm="6")

    inv = ImageOps.invert(gray)
    inv_up = inv.resize((w * 4, h * 4))
    inv_bw = inv_up.point(lambda p: 255 if p > 80 else 0)
    all_text += " " + _tesseract_raw(inv_bw, psm="6")

    text_lower = all_text.lower()
    hits = sum(1 for kw in _ANCHOR_KEYWORDS if kw in text_lower)
    return hits >= 2


def _tesseract_raw(img, psm: str = "6") -> str:
    """Run Tesseract with broad character set for detection (no whitelist)."""
    try:
        import pytesseract
        config = f"--psm {psm}"
        return pytesseract.image_to_string(img, config=config).strip()
    except Exception:
        return ""


def scan_refinery(region: dict, station: str = "") -> list[dict] | None:
    """Full refinery scan: detect panel, then extract via Paddle or Tesseract.

    Tries PaddleOCR first (fast, accurate for UI text). Falls back to
    Tesseract multi-variant if Paddle is unavailable.

    Returns:
      None — panel not visible
      [] — panel visible but no orders parsed
      [order_dict, ...] — extracted order data
    """
    from ocr.screen_reader import capture_region, is_ocr_available

    if not is_ocr_available():
        return None

    img = capture_region(region)
    if img is None:
        return None

    # Save debug screenshot
    import os
    _debug_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        img.save(os.path.join(_debug_dir, "refinery_ocr_capture.png"))
    except Exception:
        pass

    debug_path = os.path.join(_debug_dir, "refinery_ocr_debug.txt")

    # ── Try PaddleOCR first (preferred engine) ──
    paddle_regions = _engine_paddle(img)
    if paddle_regions is not None:
        # Write debug
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write("=== PADDLE OCR RESULTS ===\n\n")
                for r in paddle_regions:
                    f.write(f"  x={r.get('x_mid', '?'):>4}  "
                            f"y={r.get('y_mid', '?'):>4}  "
                            f"conf={r.get('conf', 0):.2f}  "
                            f"text={r.get('text', '')!r}\n")
                f.write("\n")
        except OSError:
            pass

        # Gate check: do the paddle results contain refinery keywords?
        all_paddle_text = " ".join(r.get("text", "") for r in paddle_regions).lower()
        hits = sum(1 for kw in _ANCHOR_KEYWORDS if kw in all_paddle_text)
        if hits < 2:
            return None

        # Parse Paddle results into structured data
        orders = _parse_paddle_results(paddle_regions)
        if orders:
            for order in orders:
                if station:
                    order["station"] = station
            return orders
        # Paddle detected the panel but couldn't parse — fall through to Tesseract

    # ── Fallback: Tesseract multi-variant ──
    # Gate check with Tesseract
    gray = img.convert("L")
    w, h = gray.size
    up = gray.resize((w * 4, h * 4))
    bw = up.point(lambda p: 255 if p > 100 else 0)
    gate_text = _tesseract_raw(bw, psm="6").lower()
    bw2 = up.point(lambda p: 255 if p > 140 else 0)
    gate_text += " " + _tesseract_raw(bw2, psm="6").lower()

    hits = sum(1 for kw in _ANCHOR_KEYWORDS if kw in gate_text)

    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(f"=== TESSERACT GATE CHECK: {hits} hits ===\n")
            f.write(f"Gate text:\n{gate_text}\n\n")
    except OSError:
        pass

    if hits < 2:
        return None

    tesseract_texts = _engine_a_full_text(img)

    try:
        with open(debug_path, "a", encoding="utf-8") as f:
            f.write("=== TESSERACT ENGINE A OUTPUTS ===\n\n")
            for i, text in enumerate(tesseract_texts):
                f.write(f"--- Variant {i} ---\n{text}\n\n")
    except OSError:
        pass

    orders = _parse_ocr_results(tesseract_texts, [])

    for order in orders:
        if station:
            order["station"] = station

    return orders
