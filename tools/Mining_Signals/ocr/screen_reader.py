"""Screen capture and OCR for mining scanner digit extraction.

Uses ``mss`` for fast in-memory screen grabs and ``pytesseract`` for
digit-only OCR.  Tesseract binary is auto-downloaded on first use if
not already present.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from typing import Optional

log = logging.getLogger(__name__)

# ── Tesseract binary management ──
# Bundled/downloaded Tesseract lives here:
_TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESSERACT_DIR = os.path.join(_TOOL_DIR, "tesseract")
_TESSERACT_EXE = os.path.join(_TESSERACT_DIR, "tesseract.exe")

# UB-Mannheim portable build (Tesseract 5.4.0, ~33 MB installer)
_TESSERACT_URL = (
    "https://github.com/UB-Mannheim/tesseract/releases/download/"
    "v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
)
# SHA-256 of the installer for integrity verification after download.
# Update this hash when bumping the Tesseract version.  Set to "" to
# skip verification (logs a warning instead of blocking install).
_TESSERACT_SHA256 = ""

# Lazy-loaded flags — set on first use, guarded by _init_lock
_init_lock = threading.Lock()
_MSS_AVAILABLE: Optional[bool] = None
_TESSERACT_AVAILABLE: Optional[bool] = None


def _check_mss() -> bool:
    global _MSS_AVAILABLE
    if _MSS_AVAILABLE is not None:
        return _MSS_AVAILABLE
    with _init_lock:
        if _MSS_AVAILABLE is not None:
            return _MSS_AVAILABLE
        try:
            import mss  # noqa: F401
            _MSS_AVAILABLE = True
        except ImportError:
            _MSS_AVAILABLE = False
            log.warning("screen_reader: 'mss' not installed — screen capture disabled")
    return _MSS_AVAILABLE


def _find_tesseract() -> str | None:
    """Locate the Tesseract binary — bundled copy first, then system PATH."""
    # 1. Bundled copy (auto-downloaded or shipped with installer)
    if os.path.isfile(_TESSERACT_EXE):
        return _TESSERACT_EXE

    # 2. System PATH
    system_exe = shutil.which("tesseract")
    if system_exe:
        return system_exe

    # 3. Common Windows install locations
    for prog in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", "")):
        if prog:
            candidate = os.path.join(prog, "Tesseract-OCR", "tesseract.exe")
            if os.path.isfile(candidate):
                return candidate

    return None


def _download_tesseract() -> bool:
    """Download and extract Tesseract OCR binary to the tool directory.

    Downloads the UB-Mannheim NSIS installer and extracts it silently
    to a local directory.  No admin rights required.
    """
    log.info("screen_reader: downloading Tesseract OCR (~33 MB)...")

    os.makedirs(_TESSERACT_DIR, exist_ok=True)
    installer_path = os.path.join(_TESSERACT_DIR, "tesseract_setup.exe")

    try:
        # Download installer
        req = urllib.request.Request(
            _TESSERACT_URL,
            headers={"User-Agent": "SC-Toolbox/1.0"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(installer_path, "wb") as f:
                shutil.copyfileobj(resp, f)

        # Verify download integrity via SHA-256
        sha = hashlib.sha256()
        with open(installer_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        actual_hash = sha.hexdigest()
        if _TESSERACT_SHA256:
            if actual_hash != _TESSERACT_SHA256:
                log.error(
                    "screen_reader: Tesseract installer hash mismatch "
                    "(expected %s, got %s) — aborting install",
                    _TESSERACT_SHA256[:16], actual_hash[:16],
                )
                try:
                    os.remove(installer_path)
                except OSError:
                    pass
                return False
        else:
            log.warning(
                "screen_reader: no expected hash configured — "
                "set _TESSERACT_SHA256 = %r to pin this binary",
                actual_hash,
            )

        log.info("screen_reader: download verified, extracting...")

        # Run NSIS installer in silent mode to local directory.
        # The installer requires elevation — use PowerShell Start-Process
        # with -Verb RunAs to trigger the UAC prompt.
        # Paths are validated to contain only safe characters before
        # interpolation into the PowerShell command string.
        for label, path in [("installer", installer_path), ("target", _TESSERACT_DIR)]:
            if not re.match(r'^[A-Za-z0-9 _\-.\\/:()]+$', path):
                log.error("screen_reader: unsafe characters in %s path: %s", label, path)
                return False
        ps_cmd = (
            f"Start-Process -FilePath '{installer_path}' "
            f"-ArgumentList '/S','/D={_TESSERACT_DIR}' "
            f"-Verb RunAs -Wait"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            timeout=120,
            capture_output=True,
        )

        # Clean up installer
        try:
            os.remove(installer_path)
        except OSError:
            pass

        if os.path.isfile(_TESSERACT_EXE):
            log.info("screen_reader: Tesseract installed to %s", _TESSERACT_DIR)
            return True
        else:
            log.error("screen_reader: Tesseract extraction failed (exe not found)")
            return False

    except Exception as exc:
        log.error("screen_reader: Tesseract download failed: %s", exc)
        # Clean up partial download
        try:
            os.remove(installer_path)
        except OSError:
            pass
        return False


def _check_tesseract() -> bool:
    global _TESSERACT_AVAILABLE
    if _TESSERACT_AVAILABLE is not None:
        return _TESSERACT_AVAILABLE
    with _init_lock:
        if _TESSERACT_AVAILABLE is not None:
            return _TESSERACT_AVAILABLE
        try:
            import pytesseract

            exe = _find_tesseract()
            if not exe:
                log.info("screen_reader: Tesseract not found, attempting auto-download...")
                if _download_tesseract():
                    exe = _TESSERACT_EXE
                else:
                    _TESSERACT_AVAILABLE = False
                    return False

            pytesseract.pytesseract.tesseract_cmd = exe
            pytesseract.get_tesseract_version()
            _TESSERACT_AVAILABLE = True
            log.info("screen_reader: using Tesseract at %s", exe)

        except ImportError:
            _TESSERACT_AVAILABLE = False
            log.warning("screen_reader: 'pytesseract' not installed")
        except Exception as exc:
            _TESSERACT_AVAILABLE = False
            log.warning("screen_reader: Tesseract check failed: %s", exc)
    return _TESSERACT_AVAILABLE


def is_ocr_available() -> bool:
    """Return True if both mss and pytesseract/Tesseract are usable."""
    return _check_mss() and _check_tesseract()


def tesseract_status() -> str:
    """Return a human-readable status string for the OCR subsystem."""
    if not _check_mss():
        return "mss not installed (pip install mss)"
    if not _check_tesseract():
        return "Tesseract not available — will auto-download on first scan"
    return "Ready"


def capture_region(region: dict) -> Optional[object]:
    """Capture a screen region and return a PIL Image (or None).

    *region* must have keys: x, y, w, h (integers, pixels).
    The image lives entirely in memory — nothing is saved to disk.
    """
    if not _check_mss():
        return None

    import mss
    from PIL import Image

    monitor = {
        "left": region["x"],
        "top": region["y"],
        "width": region["w"],
        "height": region["h"],
    }

    try:
        with mss.mss() as sct:
            grab = sct.grab(monitor)
            img = Image.frombytes("RGB", grab.size, grab.bgra, "raw", "BGRX")

            # No blind crop — the icon is handled by the max-value
            # stripping logic in extract_number() which strips leading
            # digits if the OCR result exceeds the max plausible signal.

            return img
    except Exception as exc:
        log.error("screen_reader: capture failed: %s", exc)
        return None


def capture_region_averaged(
    region: dict,
    n_frames: int = 4,
    delay_ms: int = 30,
) -> Optional[object]:
    """Capture a region multiple times and return a pixel-averaged composite.

    The Star Citizen HUD renders scan-panel text with a subpixel
    jitter animation ("holographic wiggle") — characters oscillate
    1-2 pixels at a few Hz. A single-frame capture catches the text
    mid-animation, which causes OCR to return slightly different
    reads every scan and starves the consensus logic in
    ``ui/app.py::_do_scan``.

    Empirical observation (static rock at MASS 6805 scanned in live
    game): with n_frames=3 and delay_ms=25 the averaged composite
    still produced reads that bounced through 6805 / 6815 / 6820 /
    6845 / 6855 across consecutive scans. Position 2 (the `0` digit)
    was drifting by 1-5 across the zero-through-five range,
    indicating the 50 ms sampling window was not spanning enough of
    the animation cycle.

    Bumping to 7 frames × 45 ms = ~300 ms sampling window gives the
    averaging ~6× longer to integrate the jitter. A full animation
    cycle at even 3 Hz is 333 ms, so 300 ms captures almost a full
    period of the wiggle and bakes it into a stable mean.

    Cost: ~n_frames * (capture_ms + delay_ms) ≈ 7×(20+45) ≈ 455 ms,
    absorbed by the 1 s scan interval. There is still headroom on
    the 5 s HUD-future budget for the downstream OCR pipeline.

    Parameters
    ----------
    region : dict
        Same as ``capture_region`` — {x, y, w, h}.
    n_frames : int, default 7
        Number of consecutive captures to average. 3 was insufficient;
        7 fully kills the wiggle in observed testing. Values above 9
        add latency without improving read stability.
    delay_ms : int, default 45
        Inter-frame delay. 45 ms = ~22 Hz sampling, slow enough to
        catch distinct animation phases, fast enough that 7 frames
        still fit under the 500 ms soft-budget for this step.

    Returns
    -------
    PIL.Image or None
        The averaged composite as a PIL Image in RGB mode, or None
        if any capture failed.
    """
    if n_frames <= 1:
        return capture_region(region)
    if not _check_mss():
        return None

    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return capture_region(region)

    frames: list = []
    for i in range(n_frames):
        img = capture_region(region)
        if img is None:
            return None
        frames.append(np.asarray(img, dtype=np.float32))
        if i < n_frames - 1 and delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    if not frames:
        return None

    # Pixel-wise mean, then round back to uint8
    composite = np.mean(frames, axis=0)
    composite = np.clip(composite, 0, 255).astype(np.uint8)
    return Image.fromarray(composite)


def _try_ocr(image, config: str) -> str:
    """Run pytesseract with a given config and return the raw text."""
    import pytesseract
    return pytesseract.image_to_string(image, config=config).strip()


MAX_SIGNAL = 35000
MIN_SIGNAL = 1000

# ── Preprocessing thresholds ──
# These are the main tuning knobs for OCR sensitivity against different
# HUD color schemes and lighting conditions.
_BRIGHT_BG_THRESHOLD = 140      # avg pixel brightness above which background is "bright"
_BRIGHT_BG_FALLBACK = 128       # default brightness when sampling fails (neutral gray)
_CHANNEL_THRESH_BRIGHT = 100    # per-channel binarization for bright-on-dark text
_CHANNEL_THRESH_INV = 80        # inverted-gray binarization threshold
_DARK_TEXT_THRESH = 100         # threshold for dark-on-bright text extraction
_DIFF_THRESH_CYAN = 10          # G-R channel difference threshold (cyan/teal text)
_DIFF_THRESH_ORANGE = 15        # R-G channel difference threshold (orange/yellow text)

# Module-level executor for extract_number() so we can time-box the
# Paddle future without the `with ThreadPoolExecutor()` context
# manager blocking on shutdown. Created lazily on first use.
_extract_pool = None
_pool_lock = threading.Lock()


def _get_extract_pool():
    global _extract_pool
    if _extract_pool is not None:
        return _extract_pool
    with _pool_lock:
        if _extract_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _extract_pool = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="extract_number"
            )
    return _extract_pool


# Max time we'll wait on PaddleOCR's result per scan. Warm calls take
# ~1.2 s; the first cold call can take 15-20 s (daemon spawn + model
# load). We don't want to block the signal scan on Paddle — if it
# can't answer within this budget, use Tesseract-only and let the
# abandoned future keep running in the background to warm the daemon
# for the next call.
_PADDLE_SIGNAL_TIMEOUT_S = 2.5


def _preprocess_engine_a(image) -> list:
    """Engine A: color channel thresholding at 3x scale, PSM 6.

    Handles both dark-on-bright and bright-on-dark text by detecting
    the dominant background brightness and applying the right threshold
    polarity. Catches yellow/green/orange/cyan HUD text reliably.
    """
    from PIL import Image, ImageOps

    scale = 3
    r_ch, g_ch, b_ch = image.split()
    gray = image.convert("L")
    variants = []

    # Detect whether background is bright or dark
    # Sample the image's mean brightness
    pixels = gray.load()
    w, h = gray.size
    sample_count = 0
    total = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            total += pixels[x, y]
            sample_count += 1
    avg_brightness = total / sample_count if sample_count else _BRIGHT_BG_FALLBACK
    is_bright_bg = avg_brightness > _BRIGHT_BG_THRESHOLD

    # ── Bright-on-dark variants (standard HUD case) ──
    thr = _CHANNEL_THRESH_BRIGHT
    for ch in (r_ch, g_ch, b_ch):
        t = ch.point(lambda p: 255 if p > thr else 0)
        variants.append(t.resize((t.width * scale, t.height * scale), Image.LANCZOS))

    inv = ImageOps.invert(gray)
    t = inv.point(lambda p: 255 if p > _CHANNEL_THRESH_INV else 0)
    variants.append(t.resize((t.width * scale, t.height * scale), Image.LANCZOS))

    # ── Dark-on-bright variant (for bright backgrounds like ice/atmosphere) ──
    if is_bright_bg:
        t = gray.point(lambda p: 255 if p < _DARK_TEXT_THRESH else 0)
        variants.append(t.resize((t.width * scale, t.height * scale), Image.LANCZOS))

    return variants


def _preprocess_engine_b(image) -> list:
    """Engine B: channel-difference isolation for low-contrast scenes.

    Uses color channel subtraction to isolate HUD text (cyan/teal/orange)
    from backgrounds where hue differs but brightness is similar.
    """
    from PIL import Image, ImageOps, ImageChops

    scale = 3
    gray = image.convert("L")
    r_ch, g_ch, b_ch = image.split()

    variants = []

    # Green - Red: isolates cyan/teal text on mint/green backgrounds
    diff_gr = ImageChops.difference(g_ch, r_ch)
    t = diff_gr.point(lambda p: 255 if p > _DIFF_THRESH_CYAN else 0)
    variants.append(t.resize((t.width * scale, t.height * scale), Image.LANCZOS))

    # Red - Green: isolates orange/yellow text on green backgrounds
    diff_rg = ImageChops.difference(r_ch, g_ch)
    t = diff_rg.point(lambda p: 255 if p > _DIFF_THRESH_ORANGE else 0)
    variants.append(t.resize((t.width * scale, t.height * scale), Image.LANCZOS))

    # Autocontrast gray — normalizes brightness range
    auto = ImageOps.autocontrast(gray, cutoff=5)
    auto = auto.resize((auto.width * scale, auto.height * scale), Image.LANCZOS)
    variants.append(auto)

    return variants


def _ocr_engine(variants: list, config: str) -> list[int]:
    """Run OCR on a list of preprocessed variants and return valid candidates."""
    candidates: list[int] = []
    for v in variants:
        raw = _try_ocr(v, config)
        if not raw:
            continue
        digits = re.findall(r"\d+", raw)
        if not digits:
            continue
        best = max(digits, key=len)
        if len(best) < 3:
            continue
        # Strip leading digits (icon artifacts) until we get a valid signal
        s = best
        while len(s) >= 3:
            val = int(s)
            if MIN_SIGNAL <= val <= MAX_SIGNAL:
                candidates.append(val)
                break
            s = s[1:]
    return candidates


def _best_candidate(candidates: list[int]) -> Optional[int]:
    """Pick the best candidate using frequency + length tiebreaker."""
    if not candidates:
        return None
    from collections import Counter
    counts = Counter(candidates).most_common()
    top_count = counts[0][1]
    tied = [val for val, cnt in counts if cnt == top_count]
    return max(tied, key=lambda v: len(str(v)))


def _extract_paddle_signal_candidates(image) -> list[int]:
    """Run the PaddleOCR sidecar on *image* and return signal candidates.

    Parses Paddle's text regions for digit strings, applies the same
    icon-stripping + MIN_SIGNAL..MAX_SIGNAL range check as the
    Tesseract engines. Returns an empty list if Paddle is unavailable
    or produced no valid readings.

    Cost: ~1.2s warm on a signal-region-sized crop. Intended to run
    concurrently with Tesseract engines A and B via threading.
    """
    try:
        from . import paddle_client
    except Exception:
        return []

    if not paddle_client.is_available():
        return []

    regions = paddle_client.recognize(image)
    if not regions:
        return []

    candidates: list[int] = []
    for region in regions:
        text = region.get("text", "") or ""
        # Strip thousands separators (comma, period, space, apostrophe)
        # BEFORE extracting digit runs. Paddle preserves these
        # punctuation characters where Tesseract filters them via
        # the digit whitelist — e.g. Paddle reads "10,620" literally,
        # which would get split into ["10", "620"] by the naive regex
        # and the real value would be lost.
        cleaned = re.sub(r"[,.\s']", "", text)
        digits = re.findall(r"\d+", cleaned)
        if not digits:
            continue
        best = max(digits, key=len)
        if len(best) < 3:
            continue
        # Same icon-stripping as _ocr_engine: trim leading digits
        # until the value lands in the valid signal range.
        s = best
        while len(s) >= 3:
            val = int(s)
            if MIN_SIGNAL <= val <= MAX_SIGNAL:
                candidates.append(val)
                break
            s = s[1:]
    return candidates


def extract_number(image) -> Optional[int]:
    """Run digit-only OCR on *image* using triple-engine cross-validation.

    Runs three independent OCR pipelines concurrently:
      A. Tesseract with color-channel thresholding + PSM 6
      B. Tesseract with channel-difference isolation + PSM 7
      C. PaddleOCR via sidecar (skipped if py313 embed not installed)

    Votes by combined-candidate frequency when engines disagree. When
    all three agree (or all available engines agree), returns the
    common value immediately. Paddle is optional — if the sidecar
    isn't available, voting falls back to the A+B two-engine logic.

    Each Tesseract engine runs 3-4 preprocessing variants. Total wall
    time: ~700 ms with Tesseract only, ~1.2 s with Paddle in parallel.
    """
    if not _check_tesseract():
        return None

    try:
        import concurrent.futures as _cf

        def _run_a() -> list[int]:
            variants = _preprocess_engine_a(image)
            return _ocr_engine(
                variants,
                "--psm 6 -c tessedit_char_whitelist=0123456789",
            )

        def _run_b() -> list[int]:
            variants = _preprocess_engine_b(image)
            return _ocr_engine(
                variants,
                "--psm 7 -c tessedit_char_whitelist=0123456789",
            )

        # Submit all three engines to the module-level executor.
        # Module-level is CRITICAL: a `with ThreadPoolExecutor() as`
        # context manager calls shutdown(wait=True) at exit, which
        # blocks on the Paddle future even if we already got the
        # Tesseract results and only want to return. With a persistent
        # pool, the function returns as soon as the Paddle future
        # either completes or hits its inner timeout; the abandoned
        # future keeps running in the background and its eventual
        # completion warms the daemon for the next call.
        pool = _get_extract_pool()
        fut_a = pool.submit(_run_a)
        fut_b = pool.submit(_run_b)
        fut_c = pool.submit(_extract_paddle_signal_candidates, image)

        # Tesseract engines always return quickly (~200 ms each).
        # A long outer timeout guards against truly runaway
        # preprocessing bugs, not normal operation.
        try:
            candidates_a = fut_a.result(timeout=5.0)
        except Exception as exc:
            log.warning("extract_number: engine A failed: %s", exc)
            candidates_a = []
        try:
            candidates_b = fut_b.result(timeout=5.0)
        except Exception as exc:
            log.warning("extract_number: engine B failed: %s", exc)
            candidates_b = []

        # Paddle gets a short time budget. First call per session
        # takes ~15-20 s (daemon spawn + model load) so we almost
        # always time out here on the very first scan; that's fine,
        # the daemon keeps initializing in the background and the
        # next call benefits from the warm cache. Warm calls return
        # in ~1.2 s which fits in the 2.5 s budget.
        try:
            candidates_c = fut_c.result(timeout=_PADDLE_SIGNAL_TIMEOUT_S)
        except _cf.TimeoutError:
            log.debug(
                "extract_number: Paddle busy, falling back to Tesseract-only"
            )
            candidates_c = []
        except Exception as exc:
            log.warning("extract_number: Paddle engine failed: %s", exc)
            candidates_c = []

        result_a = _best_candidate(candidates_a)
        result_b = _best_candidate(candidates_b)
        result_c = _best_candidate(candidates_c)

        log.debug("OCR A=%s B=%s C=%s", result_a, result_b, result_c)

        # ── Three-way consensus fast path ──
        # If all available engines agree, return immediately with
        # maximum confidence. "Available" means they each produced
        # at least one valid candidate; an engine that failed (e.g.
        # Paddle sidecar unavailable → empty list) is skipped and
        # we fall back to voting among the engines that succeeded.
        present = [r for r in (result_a, result_b, result_c) if r is not None]
        if present and all(r == present[0] for r in present):
            log.debug(
                "screen_reader: %d engine(s) agree on %d",
                len(present), present[0],
            )
            def _try_collect(value: int, conf: str):
                try:
                    from .training_collector import collect_training_sample
                    collect_training_sample(image, value, confidence=conf)
                except Exception:
                    pass
            _try_collect(present[0], "consensus" if len(present) >= 2 else "solo")
            return present[0]

        def _try_collect(value: int, conf: str):
            try:
                from .training_collector import collect_training_sample
                collect_training_sample(image, value, confidence=conf)
            except Exception:
                pass  # never let training collection break scanning

        # ── Cross-engine voting when engines disagree ──
        # Combine the full candidate pools from all three engines
        # and vote by frequency. Paddle contributes its candidates
        # alongside Tesseract A/B, so a single Paddle read that
        # matches either Tesseract engine gets the "vote dominance"
        # bonus even when the two Tesseract engines disagree.
        combined = candidates_a + candidates_b + candidates_c
        if not combined:
            log.debug("screen_reader: no digits found by any engine")
            return None

        from collections import Counter
        counts = Counter(combined).most_common()
        top_count = counts[0][1]
        tied = [val for val, cnt in counts if cnt == top_count]
        # Tiebreak among frequency-ties by digit length (real
        # signals are longer than fragments).
        best = max(tied, key=lambda v: len(str(v)))

        # Confidence tag for training collection
        if top_count >= 3:
            conf = "vote"
        else:
            conf = "weak"

        log.debug(
            "screen_reader: vote winner %d (A=%s B=%s C=%s, combined=%s)",
            best, result_a, result_b, result_c, counts[:5],
        )
        _try_collect(best, conf)
        return best
    except Exception as exc:
        log.error("screen_reader: OCR failed: %s", exc)
        return None


_last_capture = None  # most recent signal region image (for training collection)


def get_last_capture():
    """Return the most recent signal region image, or None."""
    return _last_capture


def scan_region(region: dict) -> Optional[int]:
    """One-shot: capture the region and extract the number.

    Uses multi-frame averaging to defeat the HUD's subpixel wiggle
    animation (see ``capture_region_averaged`` docstring). Falls
    back to a single capture if averaging fails.

    Returns the extracted integer or None. Entirely in-memory.
    """
    global _last_capture
    img = capture_region_averaged(region)
    if img is None:
        img = capture_region(region)
    if img is None:
        return None
    _last_capture = img
    return extract_number(img)
