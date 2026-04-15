"""Reservoir-sampled digit crop storage for online learning.

Stores confirmed-correct 28×28 grayscale digit images produced by
unanimous 3-engine consensus (ONNX + Tesseract + PaddleOCR). Each
digit class (0-9) has at most ``MAX_PER_CLASS`` samples on disk,
managed via Algorithm R (reservoir sampling without replacement).

Storage location:
    %LOCALAPPDATA%/SC_Toolbox/digit_reservoir/{0-9}/<ts_ms>.png

Total disk cost: ~100-250 KB (500 × ~200-500 bytes per PNG).
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Only store digit classes (not . - %)
DIGIT_CLASSES = "0123456789"

MAX_PER_CLASS = 50

_BASE_DIR = Path(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
) / "SC_Toolbox" / "digit_reservoir"


class DigitReservoir:
    """Fixed-size per-class reservoir of confirmed digit crops."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._base = base_dir or _BASE_DIR
        # In-memory bookkeeping — loaded lazily from disk
        self._counts: dict[str, int] = {}       # current files on disk
        self._total_seen: dict[str, int] = {}   # lifetime counter (for Algorithm R)
        self._ready = False

    # ── public API ───────────────────────────────────────────

    def add(self, digit_char: str, image_28x28: np.ndarray) -> bool:
        """Store a confirmed digit crop via reservoir sampling.

        Parameters
        ----------
        digit_char : str
            Single character '0'-'9'.
        image_28x28 : np.ndarray
            uint8 grayscale array of shape (28, 28).

        Returns True if the sample was actually written to disk.
        """
        if digit_char not in DIGIT_CLASSES:
            return False
        if image_28x28.shape != (28, 28):
            return False

        self._ensure_ready()

        cls = digit_char
        self._total_seen[cls] = self._total_seen.get(cls, 0) + 1
        n_seen = self._total_seen[cls]
        n_stored = self._counts.get(cls, 0)

        if n_stored < MAX_PER_CLASS:
            # Phase 1: reservoir not yet full — always add
            self._write(cls, image_28x28)
            self._counts[cls] = n_stored + 1
            return True

        # Phase 2: reservoir full — accept with probability MAX/n_seen
        # and replace a random existing sample (Algorithm R).
        j = random.randint(0, n_seen - 1)
        if j < MAX_PER_CLASS:
            self._replace_random(cls, image_28x28)
            return True

        return False

    def get_all(self) -> list[tuple[np.ndarray, int]]:
        """Load every reservoir sample as (image_28x28, class_index) pairs.

        class_index is the integer digit value (0-9), suitable for
        use as a PyTorch label.
        """
        self._ensure_ready()
        samples = []
        for cls in DIGIT_CLASSES:
            cls_dir = self._base / cls
            if not cls_dir.is_dir():
                continue
            label = int(cls)
            for png in cls_dir.glob("*.png"):
                try:
                    from PIL import Image
                    img = Image.open(png).convert("L").resize((28, 28))
                    arr = np.array(img, dtype=np.uint8)
                    samples.append((arr, label))
                except Exception:
                    continue
        return samples

    def sample_count(self) -> dict[str, int]:
        """Return current on-disk sample count per class."""
        self._ensure_ready()
        return dict(self._counts)

    def clear(self) -> None:
        """Delete all reservoir samples."""
        import shutil
        for cls in DIGIT_CLASSES:
            cls_dir = self._base / cls
            if cls_dir.is_dir():
                shutil.rmtree(cls_dir, ignore_errors=True)
        self._counts.clear()
        self._total_seen.clear()
        self._ready = False
        log.info("digit_reservoir: cleared all samples")

    # ── internals ────────────────────────────────────────────

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        for cls in DIGIT_CLASSES:
            cls_dir = self._base / cls
            cls_dir.mkdir(parents=True, exist_ok=True)
            self._counts[cls] = len(list(cls_dir.glob("*.png")))
        self._ready = True

    def _write(self, cls: str, image: np.ndarray) -> None:
        """Write a new PNG to the class directory."""
        try:
            from PIL import Image
            ts = int(time.time() * 1000)
            path = self._base / cls / f"{ts}.png"
            Image.fromarray(image, mode="L").save(path)
        except Exception as exc:
            log.debug("digit_reservoir: write failed: %s", exc)

    def _replace_random(self, cls: str, image: np.ndarray) -> None:
        """Replace a random existing sample in the class directory."""
        cls_dir = self._base / cls
        existing = list(cls_dir.glob("*.png"))
        if not existing:
            self._write(cls, image)
            return
        victim = random.choice(existing)
        try:
            victim.unlink()
        except Exception:
            pass
        self._write(cls, image)
