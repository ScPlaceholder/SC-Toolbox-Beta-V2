"""Live glyph reader — visualizes what the OCR pipeline SEES vs READS.

Polls ``debug_glyphs/latest.json`` every 500 ms. The OCR pipeline
(``ocr/sc_ocr/api.py:_classify_crops``) writes one PNG per glyph
crop plus a JSON index whenever it runs. This viewer renders each
field's glyphs as a row of upscaled images with the classifier's
output and confidence stamped underneath, color-coded by confidence:

  GREEN   conf >= 0.85   — pipeline trusts this read
  YELLOW  0.50 <= conf   — borderline; downstream confidence-gate
                            may reject it
  RED     conf < 0.50    — low confidence, classifier is guessing

You watch this side-by-side with the actual game HUD to immediately
see which digits are being misread, which glyphs the segmenter
dropped, and whether the binarization step is producing clean crops
or garbage. It's the visual companion to the ``sc_ocr.diag`` log
lines — same data, but with the actual pixels.

Both classifier paths emit data:
  * ``primary``   — the strict-confidence-gated path that returns
                    immediately if ``min(conf) >= 0.85``
  * ``secondary`` — the parallel-vote path that runs alongside
                    Tesseract / CRNN

Run with::

    python scripts/glyph_reader_viewer.py

or via ``training_data_panels/LAUNCH_GlyphReader.bat``.

Cross-process single-instance: only one viewer at a time.
"""
from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPalette, QPixmap, QRegularExpressionValidator
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)


THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
sys.path.insert(0, str(TOOL))

from ocr import training_registry  # noqa: E402

GLYPH_DIR = TOOL / "debug_glyphs"
INDEX_PATH = GLYPH_DIR / "latest.json"

# Map an OCR-pipeline field name to the training-registry kind it
# trains. Both HUD-numeric fields share the "hud" model + staging dir.
# Anything not listed (e.g. future signal pipeline fields) defaults
# to "signal" so out-of-range corrections still land somewhere sane;
# unknown kinds are dropped at save time with a warning.
FIELD_TO_KIND: dict[str, str] = {
    "mass": "hud",
    "resistance": "hud",
    "instability": "hud",
    # Mineral name — reading text (letters), not digits. Same staging
    # destination as the HUD digit fields for now; corrections from
    # the live viewer flow into the same training pool.
    "mineral": "hud",
    # Signature — signal panel digit cluster. Corrections route to
    # the signal-CNN training pool (training_data_user_sig) so they
    # improve the signal-specific model rather than the HUD one.
    "signature": "signal",
}

POLL_MS = 500
HISTORY_LEN = 8
GLYPH_DISPLAY_PX = 72  # render each 28x28 glyph at this size

# Theme — matches the rest of the toolbox tools.
ACCENT = "#33dd88"
WARN = "#ddc833"
DANGER = "#ff4444"
DIM = "#888888"
BG = "#1e1e1e"
PANEL_BG = "#2a2a2a"
FG = "#e0e0e0"


def _conf_color(conf: float) -> str:
    """Map a classifier confidence to a status color."""
    if conf >= 0.85:
        return ACCENT
    if conf >= 0.50:
        return WARN
    return DANGER


# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────


class _GlyphTile(QWidget):
    """One glyph: upscaled crop image + classified char + confidence +
    (optional) correction input that writes the original 28×28 PNG into
    the appropriate per-class training folder when the user types a
    label.

    The correction input is gated by ``enable_corrections``. End users
    running the toolbox don't train the model, so the ``fix`` text box
    is hidden in the live launcher to avoid confusing them. Developers
    invoking ``scripts/glyph_reader_viewer.py`` directly opt in via the
    ``--corrections`` flag (see ``main()``).
    """

    def __init__(self, parent=None, enable_corrections: bool = False):
        super().__init__(parent)
        self._corrections_enabled = bool(enable_corrections)
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(2)

        self._img = QLabel(self)
        self._img.setFixedSize(GLYPH_DISPLAY_PX, GLYPH_DISPLAY_PX)
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setStyleSheet(
            f"background: #111; border: 1px solid {DIM};"
        )
        v.addWidget(self._img)

        self._char = QLabel("?", self)
        cf = QFont("Consolas")
        cf.setPointSize(14)
        cf.setBold(True)
        self._char.setFont(cf)
        self._char.setAlignment(Qt.AlignCenter)
        v.addWidget(self._char)

        self._conf = QLabel("0.00", self)
        sf = QFont("Consolas")
        sf.setPointSize(8)
        self._conf.setFont(sf)
        self._conf.setAlignment(Qt.AlignCenter)
        v.addWidget(self._conf)

        # Correction input — only created when corrections are enabled
        # (developer mode). End users see just the upscaled crop +
        # classified char + confidence, no editable field.
        self._fix: Optional[QLineEdit] = None
        self._fix_default_style = ""
        self._reset_timer: Optional[QTimer] = None
        if self._corrections_enabled:
            # User types the correct label here; we save the cached
            # 28×28 PNG into the right class folder. Filtered to
            # [0-9.%] (HUD's full alphabet — signal-only fields just
            # won't accept '.' or '%').
            self._fix = QLineEdit(self)
            self._fix.setMaxLength(1)
            self._fix.setAlignment(Qt.AlignCenter)
            self._fix.setFixedWidth(GLYPH_DISPLAY_PX)
            ff = QFont("Consolas")
            ff.setPointSize(11)
            ff.setBold(True)
            self._fix.setFont(ff)
            self._fix.setValidator(QRegularExpressionValidator(
                QRegularExpression("^[0-9.%]?$"), self,
            ))
            self._fix.setPlaceholderText("fix")
            self._fix_default_style = (
                f"background: #1a1a1a; color: {FG}; "
                f"border: 1px solid {DIM}; border-radius: 2px;"
            )
            self._fix.setStyleSheet(self._fix_default_style)
            self._fix.editingFinished.connect(self._on_correction_committed)
            self._fix.returnPressed.connect(self._on_correction_committed)
            v.addWidget(self._fix)

            # Reset feedback after a short delay so the input is reusable.
            self._reset_timer = QTimer(self)
            self._reset_timer.setSingleShot(True)
            self._reset_timer.timeout.connect(self._reset_fix_input)

        # Cached glyph + provenance for save_correction().
        self._cached_pil: Optional[Image.Image] = None
        self._field: str = ""
        self._source: str = ""
        self._idx: int = -1
        self._read_char: str = ""

        # Make the tile wide enough for the labels.
        self.setFixedWidth(GLYPH_DISPLAY_PX + 8)

    def update_glyph(
        self, img_path: Path, char: str, conf: float,
        field: str = "", source: str = "", idx: int = -1,
    ) -> None:
        try:
            pil = Image.open(img_path).convert("L")
            # Cache a copy of the 28×28 source pixels — the OCR pipeline
            # overwrites `img_path` on every scan, so a delayed correction
            # would otherwise save the wrong glyph.
            self._cached_pil = pil.copy()
            # Upscale with nearest-neighbour to preserve the
            # pixelated character of the 28x28 glyph (LANCZOS would
            # blur it and hide the actual classifier input).
            scaled = pil.resize(
                (GLYPH_DISPLAY_PX, GLYPH_DISPLAY_PX), Image.NEAREST,
            )
            self._img.setPixmap(QPixmap.fromImage(ImageQt(scaled.convert("RGB"))))
        except Exception:
            self._img.setText("(load fail)")
            self._cached_pil = None
        color = _conf_color(conf)
        self._char.setText(char)
        self._char.setStyleSheet(
            f"color: {color}; background: transparent;"
        )
        self._conf.setText(f"{conf:.2f}")
        self._conf.setStyleSheet(
            f"color: {color}; background: transparent;"
        )
        self._field = field
        self._source = source
        self._idx = idx
        self._read_char = char
        # Don't clobber a partially-typed correction the user is mid-
        # editing on this tile (focus check), but do clear stale "✓"
        # markers from a prior save now that we have a fresh glyph.
        if self._fix is not None:
            if not self._fix.hasFocus() and self._fix.text() in ("", "✓"):
                self._fix.setText("")
                self._fix.setStyleSheet(self._fix_default_style)

    def _on_correction_committed(self) -> None:
        if self._fix is None:
            return
        ch = self._fix.text().strip()
        if not ch:
            return
        if self._cached_pil is None or not self._field:
            self._flash_fix_error("no glyph")
            return
        kind = FIELD_TO_KIND.get(self._field)
        if kind is None:
            self._flash_fix_error("unknown field")
            return
        try:
            spec = training_registry.get(kind)
        except Exception:
            self._flash_fix_error("no kind")
            return
        if ch not in spec.label_set:
            self._flash_fix_error("bad char")
            return
        ok = _save_correction(
            self._cached_pil, ch, spec, self._field,
            self._source, self._idx,
        )
        if ok:
            self._flash_fix_saved(ch)
        else:
            self._flash_fix_error("save fail")

    def _flash_fix_saved(self, ch: str) -> None:
        if self._fix is None or self._reset_timer is None:
            return
        self._fix.setText("✓")
        self._fix.setStyleSheet(
            f"background: #143a1a; color: {ACCENT}; "
            f"border: 1px solid {ACCENT}; border-radius: 2px;"
        )
        self._reset_timer.start(900)

    def _flash_fix_error(self, why: str) -> None:
        if self._fix is None or self._reset_timer is None:
            return
        self._fix.setStyleSheet(
            f"background: #3a1414; color: {DANGER}; "
            f"border: 1px solid {DANGER}; border-radius: 2px;"
        )
        self._fix.setToolTip(f"save failed: {why}")
        self._reset_timer.start(1500)

    def _reset_fix_input(self) -> None:
        if self._fix is None:
            return
        self._fix.setText("")
        self._fix.setStyleSheet(self._fix_default_style)
        self._fix.setToolTip("")

    def clear(self) -> None:
        self._img.clear()
        self._char.setText("·")
        self._char.setStyleSheet(f"color: {DIM}; background: transparent;")
        self._conf.setText("—")
        self._conf.setStyleSheet(f"color: {DIM}; background: transparent;")
        self._cached_pil = None
        if self._fix is not None:
            self._fix.setText("")
            self._fix.setStyleSheet(self._fix_default_style)


def _save_correction(
    pil: Image.Image, ch: str, spec, field: str, source: str, idx: int,
) -> bool:
    """Persist a manually-corrected glyph into the spec's per-class
    training folder. Returns True on success.

    Filename: ``user_glyphreader_<unix_ms>_<field>_<source>_<idx>.png``
    — long but unambiguous. The ``user_`` prefix matches the convention
    used by ``extract_labeled_glyphs._save_glyph`` so review/promote
    tooling treats these the same as crops labeled in the offline UI.
    """
    class_map = {".": "dot", "%": "pct"}
    cls = class_map.get(ch, ch)
    out_dir = spec.glyph_staging_dir / cls
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    ts_ms = int(time.time() * 1000)
    safe_field = "".join(c if c.isalnum() else "_" for c in field) or "f"
    safe_source = "".join(c if c.isalnum() else "_" for c in source) or "s"
    out = (
        out_dir
        / f"user_glyphreader_{ts_ms}_{safe_field}_{safe_source}_{idx}.png"
    )
    try:
        pil.save(out)
        return True
    except Exception:
        return False


class _FieldRow(QFrame):
    """One row showing a single (field, source) pair: header + glyph tiles."""

    def __init__(
        self,
        key_label: str,
        parent=None,
        enable_corrections: bool = False,
    ):
        super().__init__(parent)
        self._corrections_enabled = bool(enable_corrections)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"background: {PANEL_BG}; border-radius: 4px;"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # Header: field name + joined string + timestamp
        header = QHBoxLayout()
        self._name = QLabel(key_label.upper(), self)
        nf = QFont("Consolas")
        nf.setPointSize(10)
        nf.setBold(True)
        self._name.setFont(nf)
        self._name.setStyleSheet(
            f"color: {ACCENT}; background: transparent;"
        )
        header.addWidget(self._name)

        self._joined = QLabel("—", self)
        jf = QFont("Consolas")
        jf.setPointSize(14)
        jf.setBold(True)
        self._joined.setFont(jf)
        self._joined.setStyleSheet(
            f"color: {FG}; background: transparent;"
        )
        header.addWidget(self._joined, 1)

        # Mean conf badge — shown for whole-crop voters (CRNN, tess,
        # vote, winner) where there are no per-glyph confs to display.
        self._mean_conf = QLabel("", self)
        mcf = QFont("Consolas")
        mcf.setPointSize(9)
        mcf.setBold(True)
        self._mean_conf.setFont(mcf)
        self._mean_conf.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._mean_conf.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        header.addWidget(self._mean_conf)

        self._age = QLabel("", self)
        af = QFont("Consolas")
        af.setPointSize(8)
        self._age.setFont(af)
        self._age.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._age.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        header.addWidget(self._age)
        v.addLayout(header)

        # Glyph tile row (horizontal). Hidden when the entry is a
        # whole-crop voter (no per-glyph crops to render).
        self._tile_row = QHBoxLayout()
        self._tile_row.setContentsMargins(0, 0, 0, 0)
        self._tile_row.setSpacing(4)
        self._tile_row.addStretch(1)
        self._tile_row_widget = QWidget(self)
        self._tile_row_widget.setLayout(self._tile_row)
        v.addWidget(self._tile_row_widget)

        self._tiles: list[_GlyphTile] = []

    def update_field(self, entry: dict, glyph_dir: Path) -> None:
        joined = str(entry.get("joined", "—"))
        ts = float(entry.get("timestamp", 0.0))
        age_s = max(0, int(time.time() - ts))
        self._joined.setText(repr(joined))
        self._age.setText(f"{age_s}s ago")

        field = str(entry.get("field", ""))
        source = str(entry.get("source", ""))
        glyphs = entry.get("glyphs") or []

        # Whole-crop voter rows (crnn, tesseract, vote, winner) have no
        # per-glyph crops — hide the tile area and show the mean conf
        # in the header instead. CNN rows (primary, secondary) keep the
        # tile display as before.
        if not glyphs:
            self._tile_row_widget.hide()
            mc = entry.get("mean_conf")
            if mc is not None:
                color = _conf_color(float(mc))
                self._mean_conf.setText(f"conf={float(mc):.2f}")
                self._mean_conf.setStyleSheet(
                    f"color: {color}; background: transparent;"
                )
            else:
                self._mean_conf.setText("")
                self._mean_conf.setStyleSheet(
                    f"color: {DIM}; background: transparent;"
                )
            return
        # Has glyphs: standard CNN row, hide the mean-conf badge and
        # show the tile area.
        self._mean_conf.setText("")
        self._tile_row_widget.show()
        # Resize the tile pool to the right count.
        while len(self._tiles) < len(glyphs):
            tile = _GlyphTile(
                self, enable_corrections=self._corrections_enabled,
            )
            # Insert before the trailing stretch so all tiles stay
            # left-aligned.
            self._tile_row.insertWidget(len(self._tiles), tile)
            self._tiles.append(tile)
        # Hide unused tiles.
        for i, tile in enumerate(self._tiles):
            if i < len(glyphs):
                g = glyphs[i]
                tile.update_glyph(
                    glyph_dir / g.get("img", ""),
                    g.get("char", "?"),
                    float(g.get("conf", 0.0)),
                    field=field,
                    source=source,
                    idx=int(g.get("idx", i)),
                )
                tile.show()
            else:
                tile.hide()


class _SignatureCnnTile(QWidget):
    """One per-digit CNN cross-check tile for the signature row.

    Lighter-weight than ``_GlyphTile`` because the signature pipeline
    has no per-digit correction workflow (the user trains the CNN
    via the offline labeler, not the live viewer): just an upscaled
    28×28 crop, the predicted char, and a confidence number — all
    color-coded by ``_conf_color``.
    """

    TILE_PX = 84  # 28 × 3, matches the existing GLYPH_DISPLAY_PX
    # convention but a touch smaller because we want the tile row to
    # fit comfortably under the value-crop preview.

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(2)

        self._img = QLabel(self)
        self._img.setFixedSize(self.TILE_PX, self.TILE_PX)
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setStyleSheet(
            f"background: #111; border: 1px solid {DIM};"
        )
        v.addWidget(self._img)

        self._char = QLabel("?", self)
        cf = QFont("Consolas")
        cf.setPointSize(13)
        cf.setBold(True)
        self._char.setFont(cf)
        self._char.setAlignment(Qt.AlignCenter)
        v.addWidget(self._char)

        self._conf = QLabel("0.00", self)
        sf = QFont("Consolas")
        sf.setPointSize(8)
        self._conf.setFont(sf)
        self._conf.setAlignment(Qt.AlignCenter)
        v.addWidget(self._conf)

        self.setFixedWidth(self.TILE_PX + 8)

        self._last_path: Optional[Path] = None
        self._last_mtime: float = 0.0
        self._cached_pixmap: Optional[QPixmap] = None

    def update_tile(self, img_path: Path, char: str, conf: float) -> None:
        # Cached re-load (mtime-keyed) — the OCR pipeline rewrites the
        # PNG every scan, but inside one scan we don't want to thrash.
        try:
            m = img_path.stat().st_mtime if img_path.is_file() else 0.0
        except Exception:
            m = 0.0
        if m > 0.0 and (
            self._last_path != img_path or m != self._last_mtime
        ):
            try:
                pil = Image.open(img_path).convert("L")
                # Nearest-neighbour upscale — preserves the pixelated
                # look the user trained on.
                if pil.height > 0:
                    scale = max(1, self.TILE_PX // pil.height)
                    new_w = pil.width * scale
                    new_h = pil.height * scale
                    pil = pil.resize((new_w, new_h), Image.NEAREST)
                self._cached_pixmap = QPixmap.fromImage(
                    ImageQt(pil.convert("RGB"))
                )
                self._img.setPixmap(self._cached_pixmap)
                self._last_path = img_path
                self._last_mtime = m
            except Exception:
                self._img.setText("?")
                self._cached_pixmap = None

        color = _conf_color(float(conf))
        self._char.setText(char or "?")
        self._char.setStyleSheet(
            f"color: {color}; background: transparent;"
        )
        self._conf.setText(f"{float(conf):.2f}")
        self._conf.setStyleSheet(
            f"color: {color}; background: transparent;"
        )


class _SignatureRow(QFrame):
    """Dedicated row for the signal-side (signature) CRNN read.

    Mirrors :class:`_FieldRow` visually but the signal pipeline is
    end-to-end CRNN — there are no per-glyph crops to render. Instead,
    we show:

      * the FULL value crop image (the PIL image fed to CRNN), upscaled
        ~3x with nearest-neighbour so the user can see the same pixels
        the model saw;
      * the raw CRNN text + mean-confidence badge (color-coded);
      * the validated bubble value, OR the rejection reason if the
        digit-count / range / confidence gate vetoed the read;
      * a history strip of the last N reads with timestamps.

    Read-only: signature reads aren't labelable per-glyph so there's
    no "fix" input on this row.
    """

    SIG_VALUE_CROP_HEIGHT = 96

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"background: {PANEL_BG}; border-radius: 4px;"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # Header row: field name + raw CRNN text + conf badge + age.
        header = QHBoxLayout()
        self._name = QLabel("SIGNATURE (CRNN)", self)
        nf = QFont("Consolas")
        nf.setPointSize(10)
        nf.setBold(True)
        self._name.setFont(nf)
        self._name.setStyleSheet(
            f"color: {ACCENT}; background: transparent;"
        )
        header.addWidget(self._name)

        self._raw = QLabel("—", self)
        rf = QFont("Consolas")
        rf.setPointSize(14)
        rf.setBold(True)
        self._raw.setFont(rf)
        self._raw.setStyleSheet(
            f"color: {FG}; background: transparent;"
        )
        header.addWidget(self._raw, 1)

        self._conf_badge = QLabel("", self)
        cbf = QFont("Consolas")
        cbf.setPointSize(9)
        cbf.setBold(True)
        self._conf_badge.setFont(cbf)
        self._conf_badge.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._conf_badge.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        header.addWidget(self._conf_badge)

        self._age = QLabel("", self)
        af = QFont("Consolas")
        af.setPointSize(8)
        self._age.setFont(af)
        self._age.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._age.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        header.addWidget(self._age)
        v.addLayout(header)

        # Value-crop image (the full PIL crop fed into CRNN). Upscaled
        # ~3x via nearest-neighbour so digits look pixelated, not
        # smoothed — same convention as the per-glyph tiles.
        self._value_crop_lbl = QLabel(self)
        self._value_crop_lbl.setMinimumHeight(self.SIG_VALUE_CROP_HEIGHT)
        self._value_crop_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._value_crop_lbl.setStyleSheet(
            f"background: #111; border: 1px solid {DIM};"
        )
        self._value_crop_lbl.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )
        v.addWidget(self._value_crop_lbl)

        # Per-digit CNN cross-check tiles. Each tile shows the 28×28
        # crop the dual-polarity signal CNN was fed, the predicted
        # char, and the softmax-max confidence (color-coded). The row
        # only appears when the JSON includes
        # ``per_digit_classifications`` — legacy entries leave the
        # row hidden so this is purely additive.
        self._cnn_tiles_container = QWidget(self)
        self._cnn_tiles_layout = QHBoxLayout(self._cnn_tiles_container)
        self._cnn_tiles_layout.setContentsMargins(0, 0, 0, 0)
        self._cnn_tiles_layout.setSpacing(4)
        self._cnn_tiles_layout.addStretch(1)  # right-pad
        self._cnn_tiles: list[_SignatureCnnTile] = []
        self._cnn_tiles_container.hide()
        v.addWidget(self._cnn_tiles_container)

        # Validated-value line: either the int the bubble would show,
        # or "rejected: <reason>" in red. Mirrors the same conf-color
        # palette used by per-glyph tiles.
        self._verdict = QLabel("—", self)
        vf = QFont("Consolas")
        vf.setPointSize(11)
        vf.setBold(True)
        self._verdict.setFont(vf)
        self._verdict.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        v.addWidget(self._verdict)

        # History strip — last N signature reads with timestamps,
        # rendered as a single multiline label so it wraps cleanly.
        hist_label = QLabel("HISTORY", self)
        hf = QFont("Consolas")
        hf.setPointSize(8)
        hf.setBold(True)
        hist_label.setFont(hf)
        hist_label.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        v.addWidget(hist_label)

        self._history_lbl = QLabel("", self)
        hlf = QFont("Consolas")
        hlf.setPointSize(8)
        self._history_lbl.setFont(hlf)
        self._history_lbl.setStyleSheet(
            f"color: {FG}; background: transparent;"
        )
        self._history_lbl.setWordWrap(False)
        v.addWidget(self._history_lbl)

        self._last_crop_path: Optional[Path] = None
        self._last_crop_mtime: float = 0.0
        self._cached_pixmap: Optional[QPixmap] = None

    def update_signature(self, entry: dict, glyph_dir: Path) -> None:
        ts = float(entry.get("timestamp", 0.0))
        age_s = max(0, int(time.time() - ts))
        self._age.setText(f"{age_s}s ago")

        raw_text = str(entry.get("crnn_text", "") or entry.get("joined", ""))
        conf_v = entry.get("crnn_confidence")
        if conf_v is None:
            conf_v = entry.get("mean_conf")
        validated = entry.get("validated_value")
        rejection = entry.get("rejection_reason")

        # Raw CRNN text, color-coded by confidence so the user can see
        # at a glance whether the read is trustworthy.
        if conf_v is not None:
            color = _conf_color(float(conf_v))
            self._raw.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
            self._conf_badge.setText(f"conf={float(conf_v):.2f}")
            self._conf_badge.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
        else:
            self._raw.setStyleSheet(
                f"color: {FG}; background: transparent;"
            )
            self._conf_badge.setText("")
        self._raw.setText(repr(raw_text) if raw_text else "—")

        # Value-crop image (upscaled). Re-load only when the file's
        # mtime advanced — same caching trick the per-glyph tiles use.
        crop_name = str(entry.get("value_crop", "") or "")
        if crop_name:
            crop_path = glyph_dir / crop_name
            try:
                m = crop_path.stat().st_mtime if crop_path.is_file() else 0.0
            except Exception:
                m = 0.0
            if (
                m > 0.0 and (
                    self._last_crop_path != crop_path
                    or m != self._last_crop_mtime
                )
            ):
                try:
                    pil = Image.open(crop_path).convert("L")
                    # Upscale to ~3x the natural height, capped at the
                    # row's display height, with nearest-neighbour to
                    # preserve the pixelated character of the source.
                    target_h = self.SIG_VALUE_CROP_HEIGHT
                    if pil.height > 0:
                        scale = max(1, target_h // pil.height)
                        # Cap scale so very small crops don't blow up
                        # the row beyond a reasonable display size.
                        scale = min(scale, 6)
                        new_w = pil.width * scale
                        new_h = pil.height * scale
                        scaled = pil.resize(
                            (new_w, new_h), Image.NEAREST,
                        )
                    else:
                        scaled = pil
                    self._cached_pixmap = QPixmap.fromImage(
                        ImageQt(scaled.convert("RGB"))
                    )
                    self._value_crop_lbl.setPixmap(self._cached_pixmap)
                    self._last_crop_path = crop_path
                    self._last_crop_mtime = m
                except Exception:
                    self._value_crop_lbl.setText("(crop load failed)")
                    self._cached_pixmap = None

        # Per-digit CNN cross-check tiles. Only present when the OCR
        # pipeline ran the dual-polarity CNN voter on the CRNN result;
        # legacy entries leave this list absent / empty and the tile
        # row stays hidden so the row still renders correctly on
        # installs without ``model_signal_cnn.onnx``.
        per_digit = entry.get("per_digit_classifications") or []
        if per_digit:
            # Pool tile widgets to cover the new count, hide extras.
            while len(self._cnn_tiles) < len(per_digit):
                tile = _SignatureCnnTile(self._cnn_tiles_container)
                # Insert before the trailing stretch so tiles stay
                # left-aligned within the row.
                self._cnn_tiles_layout.insertWidget(
                    len(self._cnn_tiles), tile,
                )
                self._cnn_tiles.append(tile)
            for i, tile in enumerate(self._cnn_tiles):
                if i < len(per_digit):
                    pd = per_digit[i]
                    crop_name = str(pd.get("crop_path", "") or "")
                    crop_path = (
                        glyph_dir / crop_name if crop_name else glyph_dir
                    )
                    tile.update_tile(
                        crop_path,
                        str(pd.get("char", "?")),
                        float(pd.get("confidence", 0.0)),
                    )
                    tile.show()
                else:
                    tile.hide()
            self._cnn_tiles_container.show()
        else:
            # Hide all tiles + the container when no CNN data —
            # legacy entries / CNN bailed.
            for tile in self._cnn_tiles:
                tile.hide()
            self._cnn_tiles_container.hide()

        # Verdict line: validated int OR rejection reason.
        if validated is not None:
            try:
                v_int = int(validated)
                self._verdict.setText(f"validated → {v_int}")
                self._verdict.setStyleSheet(
                    f"color: {ACCENT}; background: transparent;"
                )
            except (TypeError, ValueError):
                self._verdict.setText(f"validated → {validated}")
                self._verdict.setStyleSheet(
                    f"color: {ACCENT}; background: transparent;"
                )
        else:
            reason = rejection or "no read"
            self._verdict.setText(f"rejected: {reason}")
            self._verdict.setStyleSheet(
                f"color: {DANGER}; background: transparent;"
            )

        # History strip — most-recent first, matches the viewer's
        # global HISTORY panel format.
        hist = entry.get("history") or []
        lines = []
        for h in reversed(hist[-HISTORY_LEN:]):
            try:
                h_ts = float(h.get("timestamp", 0.0))
                h_str = (
                    datetime.fromtimestamp(h_ts).strftime("%H:%M:%S")
                    if h_ts else "?"
                )
            except Exception:
                h_str = "?"
            h_raw = str(h.get("raw_text", ""))
            h_conf = h.get("mean_conf")
            h_val = h.get("validated_value")
            h_rej = h.get("rejection_reason")
            conf_str = (
                f"{float(h_conf):.2f}" if h_conf is not None else "—"
            )
            if h_val is not None:
                tail = f"→ {h_val}"
            else:
                tail = f"rej: {h_rej or '?'}"
            lines.append(
                f"{h_str}  raw={h_raw!r:>10}  conf={conf_str}  {tail}"
            )
        self._history_lbl.setText("\n".join(lines) if lines else "(no history yet)")


class GlyphReaderViewer(QWidget):
    def __init__(self, enable_corrections: bool = False):
        """Live OCR diagnostic viewer.

        ``enable_corrections``: when True, each glyph tile gets a "fix"
        text input that lets the user submit a corrected label, which
        gets saved as training data for the next model retrain. Default
        False because end users running the toolbox don't train the
        model — the input would just confuse them. Direct script
        invocation (``python scripts/glyph_reader_viewer.py``) opts in
        via the ``--corrections`` flag.
        """
        super().__init__()
        self._corrections_enabled = bool(enable_corrections)
        self.setWindowTitle("Glyph Reader — live OCR vision diagnostic")
        self.setMinimumSize(880, 620)
        self.setStyleSheet(f"background: {BG}; color: {FG};")

        self._last_mtime = 0.0
        self._field_rows: dict[str, _FieldRow] = {}
        self._history: deque = deque(maxlen=HISTORY_LEN)

        self._move_pause_until = 0.0
        self._move_pause_seconds = 0.4

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)
        self._tick()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._move_pause_until = time.monotonic() + self._move_pause_seconds

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # Header
        title = QLabel("GLYPH READER", self)
        tf = QFont("Consolas")
        tf.setPointSize(13)
        tf.setBold(True)
        title.setFont(tf)
        title.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        root.addWidget(title)

        # Help text — slightly different wording in dev mode (where the
        # "fix" input is shown) vs end-user mode (read-only diagnostic).
        if self._corrections_enabled:
            _help = (
                "Per-glyph view of what the OCR pipeline sees and how "
                "it classifies each crop. Updates every 500 ms.\n"
                "Color: green ≥ 0.85 conf · yellow ≥ 0.50 · red < 0.50 "
                "(low conf → downstream gate likely rejects).\n"
                "Type the correct char in the box under any wrong "
                "glyph and press Enter — the 28×28 crop is saved into "
                "the matching training/<class>/ folder for the next "
                "retrain."
            )
        else:
            _help = (
                "Per-glyph view of what the OCR pipeline sees and how "
                "it classifies each crop. Updates every 500 ms.\n"
                "Color: green ≥ 0.85 conf · yellow ≥ 0.50 · red < 0.50 "
                "(low conf → downstream gate likely rejects)."
            )
        sub = QLabel(_help, self)
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {DIM}; font-size: 9pt; background: transparent;"
        )
        root.addWidget(sub)

        self._status_lbl = QLabel("waiting for first scan…", self)
        self._status_lbl.setStyleSheet(
            f"color: {DIM}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        root.addWidget(self._status_lbl)

        # Scrollable region for field rows.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"background: {BG};")
        wrapper = QWidget()
        wrapper.setStyleSheet(f"background: {BG};")
        self._rows_layout = QVBoxLayout(wrapper)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        self._rows_layout.addStretch(1)
        scroll.setWidget(wrapper)
        root.addWidget(scroll, 1)

        # History panel
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {DIM}; background: {DIM};")
        root.addWidget(sep)

        hist_label = QLabel("HISTORY", self)
        hf = QFont("Consolas")
        hf.setPointSize(9)
        hf.setBold(True)
        hist_label.setFont(hf)
        hist_label.setStyleSheet(
            f"color: {DIM}; background: transparent;"
        )
        root.addWidget(hist_label)

        self._history_lbl = QLabel("", self)
        self._history_lbl.setStyleSheet(
            f"color: {FG}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        self._history_lbl.setWordWrap(False)
        root.addWidget(self._history_lbl)

    # ──────────────────────────────────────────
    # Polling + render
    # ──────────────────────────────────────────

    def _tick(self) -> None:
        # Skip while user is dragging the window.
        if time.monotonic() < self._move_pause_until:
            return
        # Mark the diagnostic heartbeat so the OCR pipeline keeps
        # writing the per-glyph PNGs + voter index this viewer reads.
        # If we don't touch this, the pipeline no-ops every dump call
        # and we'd see stale data.
        try:
            from ocr.sc_ocr import debug_overlay as _dbg
            _dbg.viewer_heartbeat()
        except Exception:
            pass
        if not INDEX_PATH.is_file():
            self._status_lbl.setText(
                f"(no data yet — waiting for {INDEX_PATH.name} from "
                "the OCR pipeline)"
            )
            return
        try:
            mtime = INDEX_PATH.stat().st_mtime
        except Exception:
            return
        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                index = json.load(f)
        except Exception as exc:
            self._status_lbl.setText(f"(read failed: {exc})")
            return
        self._render(index)

    def _render(self, index: dict) -> None:
        ts = float(index.get("timestamp", 0.0))
        ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
        delta = max(0, int(time.time() - ts)) if ts else -1
        fields = index.get("fields") or {}
        self._status_lbl.setText(
            f"latest: {ts_str}  ({delta}s ago)  ·  "
            f"{len(fields)} field/source entries"
        )

        # Display order:
        #   field group: mineral → mass → resistance → instability
        #                → signature (signal-side CRNN, last because it
        #                comes from the signal pipeline not the HUD)
        #   within each: primary → secondary → crnn → tesseract → vote
        #                → winner (the value the pipeline actually
        #                returned). Primary/secondary show per-glyph
        #                tiles; the rest are whole-crop voters with
        #                text + mean conf only.
        # The ``signature_crnn`` voter entry is skipped here — its
        # contents are already rendered inside the dedicated
        # ``_SignatureRow`` widget (driven by ``signature_winner``) so
        # showing both would just duplicate the CRNN read.
        ordered_keys = sorted(
            (k for k in fields.keys() if k != "signature_crnn"),
            key=lambda k: (
                {
                    "mineral": 0,
                    "mass": 1, "resistance": 2, "instability": 3,
                    "signature": 4,
                }.get(fields[k].get("field", k), 99),
                {
                    "primary": 0, "secondary": 1,
                    "crnn": 2, "tesseract": 3,
                    "vote": 4, "winner": 5,
                }.get(fields[k].get("source", "primary"), 99),
            ),
        )

        for key in ordered_keys:
            entry = fields[key]
            # Signature uses a specialized row (full value crop +
            # validated/rejected verdict + history strip). All other
            # fields use the standard per-glyph / voter row.
            if key == "signature_winner":
                row = self._field_rows.get(key)
                if row is None:
                    row = _SignatureRow(self)
                    self._rows_layout.insertWidget(
                        len(self._field_rows), row,
                    )
                    self._field_rows[key] = row
                row.update_signature(entry, GLYPH_DIR)
                continue
            display_key = (
                f"{entry.get('field', key)} ({entry.get('source', '')})"
            )
            row = self._field_rows.get(key)
            if row is None:
                row = _FieldRow(
                    display_key, self,
                    enable_corrections=self._corrections_enabled,
                )
                # Insert before the trailing stretch.
                self._rows_layout.insertWidget(
                    len(self._field_rows), row,
                )
                self._field_rows[key] = row
            row.update_field(entry, GLYPH_DIR)

        # History line: append a one-liner per scan.
        line = "  ".join(
            f"{fields[k].get('field', k)[:4]}={fields[k].get('joined', '?'):>6}"
            for k in ordered_keys
        )
        ts_h = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
        self._history.append(f"{ts_h}  {line}")
        self._history_lbl.setText("\n".join(self._history))


# ─────────────────────────────────────────────────────────────
# Icons mode — viewer for training_data_user_sig/icon/
# ─────────────────────────────────────────────────────────────
#
# A separate viewing mode (selected via ``--mode icons``) that loads
# every PNG in the icon training folder, runs the geometric structural
# validator (``find_icon_by_geometry``) on each, and renders a tiled
# gallery showing:
#
#   * the icon image upscaled
#   * a per-check breakdown ("✓ teardrop_has_hole, ✗ oval_has_notch", ...)
#   * a red border on tiles that fail the validator overall (≤ score
#     threshold). Those are bad training data candidates.
#
# A summary line shows the pass/fail count.

# Default icon-folder candidates, tried in order. The production tree
# uses ``training_data_user_sig/icon/``; the WingmanAI roaming clone
# uses the same relative path under its own root. A ``--icon-dir``
# CLI override lets the developer point at any directory of icons.
_DEFAULT_ICON_DIRS = (
    TOOL / "training_data_user_sig" / "icon",
    Path(
        r"C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_user_sig\icon"
    ),
    TOOL / "training_data_pending_review_signal" / "icon",
)

# Visual sizing for the icon-mode tiles. Larger than the glyph tiles
# because each icon needs the per-check breakdown next to it (which
# wraps to multiple lines).
ICON_TILE_PX = 96
ICON_TILE_GRID_COLS = 4


def _resolve_icon_dir(override: Optional[str]) -> Optional[Path]:
    """Return the first existing icon directory.

    Order: explicit ``override`` (CLI flag), production tree default,
    WingmanAI roaming default, pending-review default. Returns None
    when nothing exists so the caller can render an error message.
    """
    if override:
        p = Path(override)
        if p.is_dir():
            return p
    for cand in _DEFAULT_ICON_DIRS:
        if cand.is_dir():
            return cand
    return None


def _evaluate_icon(path: Path) -> dict:
    """Run ``find_icon_by_geometry`` and synthesize a per-check breakdown.

    Returns a dict with the icon's RGB image, the validator result (or
    None), the merged ``checks`` dict, the integer score, and a top-
    level ``passed`` boolean.

    The validator's threshold is captured in ``icon_geometry.SCORE_THRESHOLD``;
    when the validator returns None, we still want to show the user
    WHICH checks failed. We re-run a simplified per-check evaluation
    against the warm mask in that case so the failure modes (e.g.
    "color_warm: ✗") are still visible. When the validator returns a
    result, its ``details.checks`` dict already gives us the per-check
    pass/fail.
    """
    try:
        pil = Image.open(path).convert("RGB")
    except Exception as exc:
        return {
            "path": path,
            "error": f"open failed: {exc}",
            "rgb": None,
            "checks": {},
            "passed": False,
            "score": 0,
        }

    import numpy as _np
    rgb = _np.asarray(pil, dtype=_np.uint8)
    try:
        # Local import to keep the GlyphReaderViewer cold-start lean
        # — find_icon_by_geometry pulls scipy.ndimage which adds
        # ~70 ms to the viewer's launch even when icons mode isn't
        # used.
        from hud_tracker.anchors.icon_geometry import (
            find_icon_by_geometry,
        )
        result = find_icon_by_geometry(rgb)
    except Exception as exc:
        return {
            "path": path,
            "error": f"validator raised: {exc}",
            "rgb": rgb,
            "checks": {},
            "passed": False,
            "score": 0,
        }

    if result is None:
        # No component scored at or above SCORE_THRESHOLD. We don't
        # have per-check details for the (likely sub-threshold) best
        # candidate the validator considered, so we surface a stub
        # "no detection" record. Tiles in this state get the red
        # border and the breakdown line just shows "✗ no detection".
        return {
            "path": path,
            "error": None,
            "rgb": rgb,
            "checks": {},  # no per-check info available
            "passed": False,
            "score": 0,
            "details": None,
        }

    details = result.get("details", {}) or {}
    checks = dict(details.get("checks", {}))
    score = int(details.get("score", 0))
    return {
        "path": path,
        "error": None,
        "rgb": rgb,
        "checks": checks,
        "passed": True,
        "score": score,
        "details": details,
        "bbox": result.get("bbox"),
        "confidence": float(result.get("confidence", 0.0)),
    }


class _IconTile(QFrame):
    """One tile: icon image + per-check breakdown + visual fail marker.

    Tiles where the validator rejected the sample get a 2 px red border
    + dim background. The per-check breakdown is rendered as a
    multi-line label with ✓ (accent green) / ✗ (red) glyphs in front
    of each check name so the user can see at a glance which structural
    test each icon failed.
    """

    # Display order for the per-check breakdown — matches the order
    # used in ``icon_geometry.find_icon_by_geometry`` so the tile
    # tracks the validator's evaluation top-to-bottom.
    _CHECK_ORDER = (
        "color_warm",
        "two_components",
        "teardrop_has_hole",
        "oval_below_teardrop",
        "oval_has_notch",
        "aspect_ratio_global",
    )

    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        passed = bool(record.get("passed"))
        # Border + background convey pass/fail at a glance.
        if passed:
            self.setStyleSheet(
                f"background: {PANEL_BG}; "
                f"border: 1px solid {DIM}; border-radius: 4px;"
            )
        else:
            # Bad training data candidate — red border, dimmer bg.
            self.setStyleSheet(
                f"background: #2a1414; "
                f"border: 2px solid {DANGER}; border-radius: 4px;"
            )

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(3)

        # Filename header — truncated.
        name = record["path"].name
        if len(name) > 30:
            name = name[:14] + "…" + name[-13:]
        nlbl = QLabel(name, self)
        nf = QFont("Consolas")
        nf.setPointSize(8)
        nf.setBold(True)
        nlbl.setFont(nf)
        nlbl.setStyleSheet(
            f"color: {DIM}; background: transparent; border: none;"
        )
        v.addWidget(nlbl)

        # Icon image — upscaled with nearest-neighbour to keep the
        # pixelated render visible.
        img_lbl = QLabel(self)
        img_lbl.setFixedSize(ICON_TILE_PX, ICON_TILE_PX)
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setStyleSheet(
            f"background: #111; border: 1px solid {DIM};"
        )
        rgb = record.get("rgb")
        if rgb is not None:
            try:
                pil = Image.fromarray(rgb)
                if pil.height > 0 and pil.width > 0:
                    scale = max(1, ICON_TILE_PX // max(pil.height, pil.width))
                    new_w = pil.width * scale
                    new_h = pil.height * scale
                    pil = pil.resize((new_w, new_h), Image.NEAREST)
                img_lbl.setPixmap(QPixmap.fromImage(ImageQt(pil)))
            except Exception:
                img_lbl.setText("(load fail)")
        else:
            img_lbl.setText("(none)")
        v.addWidget(img_lbl)

        # Score header — "score N/6  conf X.XX" or "no detection".
        if record.get("error"):
            hdr_text = f"err: {record['error']}"
            hdr_color = DANGER
        elif record.get("details") is None:
            hdr_text = "no detection"
            hdr_color = DANGER
        else:
            score = int(record.get("score", 0))
            conf = float(record.get("confidence", 0.0))
            hdr_text = f"score {score}/6 · conf {conf:.2f}"
            hdr_color = ACCENT if passed else DANGER
        hdr = QLabel(hdr_text, self)
        hf = QFont("Consolas")
        hf.setPointSize(9)
        hf.setBold(True)
        hdr.setFont(hf)
        hdr.setStyleSheet(
            f"color: {hdr_color}; background: transparent; border: none;"
        )
        v.addWidget(hdr)

        # Per-check breakdown.
        checks = record.get("checks") or {}
        if not checks:
            blank = QLabel("(no per-check details)", self)
            blf = QFont("Consolas")
            blf.setPointSize(8)
            blank.setFont(blf)
            blank.setStyleSheet(
                f"color: {DIM}; background: transparent; border: none;"
            )
            v.addWidget(blank)
        else:
            for cname in self._CHECK_ORDER:
                if cname not in checks:
                    continue
                ok = bool(checks[cname])
                glyph = "✓" if ok else "✗"
                color = ACCENT if ok else DANGER
                row = QLabel(f"{glyph} {cname}", self)
                rf = QFont("Consolas")
                rf.setPointSize(8)
                row.setFont(rf)
                row.setStyleSheet(
                    f"color: {color}; background: transparent; "
                    f"border: none;"
                )
                v.addWidget(row)

        v.addStretch(1)
        self.setFixedWidth(ICON_TILE_PX + 100)


class IconViewer(QWidget):
    """Standalone viewer for the icon training folder.

    Shown when ``glyph_reader_viewer.py`` is invoked with
    ``--mode icons``. Independent of ``GlyphReaderViewer`` (which polls
    the live OCR pipeline's debug-glyphs JSON); this loads icons from
    disk once at construction time and renders a static tiled grid.
    """

    def __init__(self, icon_dir_override: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Icon Viewer — geometric validator breakdown")
        self.setMinimumSize(960, 720)
        self.setStyleSheet(f"background: {BG}; color: {FG};")

        self._icon_dir = _resolve_icon_dir(icon_dir_override)
        self._records: list[dict] = []
        self._build_ui()
        self._load_and_render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title = QLabel("ICON VIEWER", self)
        tf = QFont("Consolas")
        tf.setPointSize(13)
        tf.setBold(True)
        title.setFont(tf)
        title.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        root.addWidget(title)

        sub = QLabel(
            "Each tile shows one icon training sample. The geometric "
            "validator (find_icon_by_geometry) runs on each and the "
            "per-check pass/fail is shown below the image. ✓ = passed, "
            "✗ = failed. Tiles with a red border failed overall (bad "
            "training data candidates).",
            self,
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"color: {DIM}; font-size: 9pt; background: transparent;"
        )
        root.addWidget(sub)

        self._status_lbl = QLabel("loading…", self)
        sf = QFont("Consolas")
        sf.setPointSize(10)
        sf.setBold(True)
        self._status_lbl.setFont(sf)
        self._status_lbl.setStyleSheet(
            f"color: {FG}; background: transparent;"
        )
        root.addWidget(self._status_lbl)

        # Scroll area with a grid of tiles.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"background: {BG};")
        wrapper = QWidget()
        wrapper.setStyleSheet(f"background: {BG};")
        self._grid = QGridLayout(wrapper)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        scroll.setWidget(wrapper)
        root.addWidget(scroll, 1)

    def _load_and_render(self) -> None:
        if self._icon_dir is None:
            self._status_lbl.setText(
                "icon directory not found (looked in: "
                + ", ".join(str(p) for p in _DEFAULT_ICON_DIRS)
                + ")"
            )
            self._status_lbl.setStyleSheet(
                f"color: {DANGER}; background: transparent;"
            )
            return

        png_paths = sorted(self._icon_dir.glob("*.png"))
        if not png_paths:
            self._status_lbl.setText(
                f"no PNGs found in {self._icon_dir}"
            )
            self._status_lbl.setStyleSheet(
                f"color: {WARN}; background: transparent;"
            )
            return

        # Cap: 600 augmentations would render 600 tiles which is too
        # many for a Qt viewport without virtualization. Cap at 200
        # so the grid stays responsive.
        MAX_TILES = 200
        if len(png_paths) > MAX_TILES:
            self._cap_msg = (
                f" (showing first {MAX_TILES} of {len(png_paths)})"
            )
            png_paths = png_paths[:MAX_TILES]
        else:
            self._cap_msg = ""

        # Evaluate each icon.
        records: list[dict] = []
        for p in png_paths:
            records.append(_evaluate_icon(p))
        self._records = records

        # Summary
        n_pass = sum(1 for r in records if r.get("passed"))
        n_fail = len(records) - n_pass
        self._status_lbl.setText(
            f"{self._icon_dir}{self._cap_msg}  ·  "
            f"{n_pass} of {len(records)} icons pass all 6 checks; "
            f"{n_fail} fail."
        )
        # Color the status by overall pass rate.
        if n_fail == 0:
            self._status_lbl.setStyleSheet(
                f"color: {ACCENT}; background: transparent;"
            )
        elif n_fail < len(records) // 4:
            self._status_lbl.setStyleSheet(
                f"color: {WARN}; background: transparent;"
            )
        else:
            self._status_lbl.setStyleSheet(
                f"color: {DANGER}; background: transparent;"
            )

        # Render tiles in a grid.
        for i, rec in enumerate(records):
            tile = _IconTile(rec, self)
            r, c = divmod(i, ICON_TILE_GRID_COLS)
            self._grid.addWidget(tile, r, c)


def main() -> int:
    # Direct script invocation defaults to dev mode (corrections
    # enabled) so the developer can label glyphs for the next retrain.
    # Pass --no-corrections to run in end-user (read-only) mode for
    # testing how players will see the viewer.
    #
    # Two top-level modes:
    #   --mode glyphs (default) — live OCR pipeline glyph reader
    #   --mode icons            — static gallery of icon training data
    #                             with per-check structural validator
    #                             breakdown
    enable_corrections = "--no-corrections" not in sys.argv

    mode = "glyphs"
    icon_dir_override: Optional[str] = None
    argv = list(sys.argv)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--mode="):
            mode = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--icon-dir" and i + 1 < len(argv):
            icon_dir_override = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--icon-dir="):
            icon_dir_override = tok.split("=", 1)[1]
            i += 1
            continue
        i += 1

    app = QApplication.instance() or QApplication(sys.argv)
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(FG))
    palette.setColor(QPalette.Base, QColor("#2a2a2a"))
    palette.setColor(QPalette.Text, QColor(FG))
    palette.setColor(QPalette.Button, QColor("#444"))
    palette.setColor(QPalette.ButtonText, QColor(FG))
    app.setPalette(palette)

    # Pick the viewer class for the requested mode.
    if mode == "icons":
        win = IconViewer(icon_dir_override=icon_dir_override)
        instance_name = "icon_viewer"
    else:
        win = GlyphReaderViewer(enable_corrections=enable_corrections)
        instance_name = "glyph_reader"

    # Cross-process single-instance: only one viewer at a time.
    # Icons mode uses a different lock name so it can coexist with
    # the live glyph reader (the developer often wants both open).
    import importlib as _il
    _il.invalidate_caches()
    from mining_shared.single_instance import SingleInstance
    guard = SingleInstance(instance_name, win)
    if not guard.acquire():
        return 0
    win._single_instance = guard

    win.show()
    win.raise_()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
