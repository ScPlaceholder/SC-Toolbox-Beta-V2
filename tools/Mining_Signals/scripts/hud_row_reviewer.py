"""HUD Row Reviewer - verify labeled HUD captures before feeding them
to the HUD-specific RGB CRNN.

Counterpart to ``row_reviewer.py`` (signatures) but for the mining
HUD's value rows: ``mass`` / ``resistance`` / ``instability``.

Walks every labeled HUD panel capture (``<capture>.json`` sidecar
with ``schema in {region1, region2}`` and at least one numeric field
labeled), extracts the per-row value crops via the production
pipeline (``find_label_positions`` + ``_find_value_crop``), and
shows them alongside the user-typed labels for a quick visual
verdict.

Why this exists: the production crop pipeline isn't perfect — when
the label-NCC match returns a slightly wrong y-band, the value crop
contains the wrong row's content. Training the CRNN on those bad
(crop, label) pairs hurts model accuracy. The reviewer lets the
user quickly spot mismatches and mark them so the training script
can skip them.

Persists per-field verdicts to the sidecar JSON::

    {
        "schema": "region1",
        "mass": "3577",
        "resistance": "0",
        "instability": "8.01",
        ...,
        "review_status_mass":        "approved" | "rejected" | "pending",
        "review_status_resistance":  "approved" | "rejected" | "pending",
        "review_status_instability": "approved" | "rejected" | "pending"
    }

Downstream (``extract_hud_value_crops.py`` and the CRNN trainer)
filter by ``review_status_<field> == "approved"`` so only verified
data feeds training.

Keyboard shortcuts:
    1/2/3       Toggle approval for mass / resistance / instability
    A           Approve ALL THREE fields
    R           Reject ALL THREE fields
    Ctrl+Enter  Save current verdict and advance
    PgUp        Previous capture
    PgDn        Next (no save)
    Esc         Quit

Run:
    python scripts/hud_row_reviewer.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QFont, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget, QProgressBar,
)


THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent

# Make the tool tree importable so we can call production crop pipeline.
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

# Production crop pipeline + glyph segmenter.
from ocr.sc_ocr import label_match as _lm           # noqa: E402
from ocr.sc_ocr import api as _api                  # noqa: E402
from ocr import onnx_hud_reader as _hud             # noqa: E402

# Theme (mirrors signature row_reviewer).
ACCENT = "#33dd88"
RED = "#ff4444"
DIM = "#888888"
BG = "#1e1e1e"
FG = "#e0e0e0"
WARN = "#ffc107"
BLUE = "#5fa8d3"

FIELDS = ("mass", "resistance", "instability")

# Where labeled HUD captures live. Both the WingmanAI tree (where
# the user typically labels) and the dev tree are scanned so this
# tool works from either install.
PANEL_ROOT_CANDIDATES = [
    Path(r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
         r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
         r"\training_data_panels"),
    TOOL / "training_data_panels",
]


def _find_all_sidecars() -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for root in PANEL_ROOT_CANDIDATES:
        if not root.is_dir():
            continue
        for sc in sorted(root.rglob("*.json")):
            if "glyphs" in sc.name or "boxes" in sc.name:
                continue
            if sc in seen:
                continue
            seen.add(sc)
            try:
                d = json.loads(sc.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("schema") not in ("region1", "region2"):
                continue
            if not any(d.get(k) for k in FIELDS):
                continue
            png = sc.with_suffix(".png")
            if not png.is_file():
                continue
            out.append(sc)
    return out


def _load_sidecar(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_sidecar(path: Path, doc: dict) -> bool:
    try:
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _extract_value_crops(
    img: Image.Image,
) -> dict[str, tuple[Optional[Image.Image], list[tuple[int, int, int, int]]]]:
    """Return ``{field: (PIL crop, per-glyph boxes)}`` using the
    PRODUCTION pipeline.

    Mirrors what ``scan_hud_onnx`` does in ``ocr/sc_ocr/api.py``:
      1. ``_find_label_rows(img)`` returns ``{field: (y_start, y_end,
         label_right)}`` using whichever tier fires first (Tier A NCC,
         Tier B measured bands, Tier C title-anchored proportional).
      2. For each field, call ``_find_value_crop(img, gray, y1, y2,
         x_min=label_right + 6)``.
      3. Run the production per-glyph segmenter ``_segment_glyphs``
         on the value crop to produce per-glyph bboxes for overlay.

    Per-glyph boxes are in CROP COORDINATES (relative to the value
    crop, not the panel image). Returned as ``[(x, y, w, h), ...]``
    matching ``_segment_glyphs``'s output convention.
    """
    gray = np.array(img.convert("L"), dtype=np.uint8)
    try:
        label_rows = _hud._find_label_rows(img)
    except Exception:
        return {f: (None, []) for f in FIELDS}
    out: dict[str, tuple[Optional[Image.Image], list[tuple[int, int, int, int]]]] = {}
    for field in FIELDS:
        entry = label_rows.get(field)
        if entry is None:
            out[field] = (None, [])
            continue
        try:
            y1, y2, label_right = entry
        except (TypeError, ValueError):
            out[field] = (None, [])
            continue
        if y2 <= y1 or (y2 - y1) < 6:
            out[field] = (None, [])
            continue
        x_min = min(img.width - 8, int(label_right) + 6)
        try:
            crop = _hud._find_value_crop(
                img, gray, int(y1), int(y2), x_min=x_min,
            )
        except Exception:
            crop = None
        if crop is None:
            out[field] = (None, [])
            continue
        # Run the same per-glyph segmenter the per-glyph CNN voter
        # uses (production line 6672 in _ocr_value_crop). Returns
        # crop-relative (x, y, w, h) bboxes that tile across the
        # detected glyph positions.
        boxes: list[tuple[int, int, int, int]] = []
        try:
            crop_gray = np.asarray(crop.convert("L"), dtype=np.uint8)
            crop_canon = _api._canonicalize_polarity(crop_gray)
            crop_bin = _api._adaptive_binarize(crop_canon)
            _, segmenter_boxes = _api._segment_glyphs(crop_canon, crop_bin)
            boxes = [
                (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
                for b in segmenter_boxes
            ]
        except Exception:
            boxes = []
        out[field] = (crop, boxes)
    return out


def _expected_segments(label_text: str) -> str:
    """Characters the segmenter should produce — one per glyph.

    Strips formatting the HUD renders but the production segmenter
    ignores:
      * "," in masses (thousands separator — too narrow to clear the
        strict-floor in ``_segment_glyphs``)
      * trailing "%" on resistance (varies per engine; we don't
        count it as a segmenter target — the production CRNN reads
        the value pre-%)
      * leading / trailing whitespace
    """
    s = (label_text or "").strip()
    s = s.rstrip("%").rstrip()
    s = s.replace(",", "")
    return s


def _render_crop_with_glyph_boxes(
    crop: Image.Image,
    boxes: list[tuple[int, int, int, int]],
    label: str,
    scale: int = 3,
) -> Image.Image:
    """Upscale the crop by ``scale`` and overlay per-glyph bboxes +
    expected-character labels above each box, plus per-box width in
    a footer strip.

    Boxes are drawn at the segmenter's REAL ``(x, y, w, h)`` extents
    so vertical drift (segmenter grabbing the wrong y-band) shows up
    as glyphs floating above or below where the digits actually sit
    in the crop. Per-box widths in the footer surface size anomalies:
    in a healthy 5-digit mass like "17,020", widths should be ~11 px
    per digit except the leading "1" at ~7 px; a missed comma or a
    fused pair shows up as a 25-px-wide box.

    When the segmented count doesn't match ``_expected_segments(label)``
    extra boxes render as red "?" and trailing expected characters go
    unrendered — both visual signals of a count mismatch.
    """
    # Top band: expected character above each box.
    # Bottom band: per-box pixel width (size diagnostic).
    LABEL_BAND_PX = 18   # pre-scale; the scale-divider below keeps it readable
    SIZE_BAND_PX = 14    # smaller — supporting info, not primary
    expected = _expected_segments(label)

    scaled_crop = crop.resize(
        (max(1, crop.width * scale), max(1, crop.height * scale)),
        Image.NEAREST,
    ).convert("RGBA")
    band_top_h = LABEL_BAND_PX * scale // 3
    band_bot_h = SIZE_BAND_PX * scale // 3
    out = Image.new(
        "RGBA",
        (scaled_crop.width, scaled_crop.height + band_top_h + band_bot_h),
        (0, 0, 0, 255),
    )
    out.paste(scaled_crop, (0, band_top_h))
    draw = ImageDraw.Draw(out)

    # Fonts: larger for expected-char header, smaller for size footer.
    font_top_px = max(12, band_top_h - 4)
    font_bot_px = max(9, band_bot_h - 3)
    def _load_font(px: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype("consola.ttf", px)
        except Exception:
            try:
                return ImageFont.truetype("arial.ttf", px)
            except Exception:
                return ImageFont.load_default()
    font_top = _load_font(font_top_px)
    font_bot = _load_font(font_bot_px)

    COLOR_BOX = (51, 221, 136, 230)        # green — matches expected count
    COLOR_EXTRA = (255, 68, 68, 230)       # red   — beyond the expected count
    crop_top_y = band_top_h                 # crop region starts here in OUT
    crop_bot_y = band_top_h + scaled_crop.height
    size_y = crop_bot_y + 1                 # footer starts just below the crop

    for i, (bx, by, bw, bh) in enumerate(boxes):
        x1 = bx * scale
        x2 = (bx + bw) * scale
        y1 = crop_top_y + by * scale
        y2 = crop_top_y + (by + bh) * scale
        in_expected = i < len(expected)
        color = COLOR_BOX if in_expected else COLOR_EXTRA

        # Real per-glyph bbox (tight — uses the segmenter's actual
        # y/h so vertical mis-grabs are visible).
        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

        # Expected character centered above the column.
        ch = expected[i] if in_expected else "?"
        try:
            text_w = draw.textlength(ch, font=font_top)
        except Exception:
            text_w = font_top_px // 2
        tx = (x1 + x2) // 2 - int(text_w) // 2
        draw.text((tx, 0), ch, fill=color, font=font_top)

        # Per-box pixel size (pre-scale dims) centered below the
        # column — surfaces size anomalies (fused pairs, dot picked
        # up as full-height digit, missed leading "1", etc.).
        size_label = f"{bw}x{bh}"
        try:
            size_w = draw.textlength(size_label, font=font_bot)
        except Exception:
            size_w = font_bot_px * len(size_label) // 2
        sx = (x1 + x2) // 2 - int(size_w) // 2
        draw.text((sx, size_y), size_label, fill=color, font=font_bot)

    return out.convert("RGB")


def _norm_label(raw: Optional[str]) -> str:
    """Display-friendly label string."""
    if raw is None:
        return ""
    return str(raw).strip()


# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────

class _FieldRow(QFrame):
    """One row showing field name + value crop + label + verdict button.

    Layout (left -> right):

        [ FIELD ]  [        crop (left-aligned)        ]  [ "label" ]  [ verdict ]

    The crop is now LEFT-aligned with no big black bar — easier to
    eyeball whether the crop actually contains the expected digits.
    The user-typed label is shown LARGE and adjacent to the crop so
    you can see at-a-glance whether they match.
    """

    def __init__(self, field: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.field = field
        self.verdict = "pending"   # "approved" | "rejected" | "pending"
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"background-color: {BG};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(12)

        # Field name (narrower; was 96 px).
        self.lbl_field = QLabel(field.upper())
        self.lbl_field.setStyleSheet(
            f"color: {BLUE}; font-weight: 600; font-size: 13px;"
        )
        self.lbl_field.setFixedWidth(88)
        lay.addWidget(self.lbl_field)

        # Glyph-count badge: "5/5 ✓" (green when segmented count
        # matches the expected-character count) or "3/5 ✗" (red on
        # mismatch). Provides a fast-scan accuracy axis independent
        # of the bbox visual and the numeric-value label — three
        # ways to assess the capture's pipeline health from one row.
        self.lbl_count = QLabel("—")
        self.lbl_count.setFixedWidth(76)
        self.lbl_count.setAlignment(Qt.AlignCenter)
        self.lbl_count.setStyleSheet(
            f"color: {DIM}; font-family: Consolas, monospace; "
            "font-size: 12px; font-weight: 700;"
        )
        lay.addWidget(self.lbl_count)

        # Crop preview: left-aligned, auto-sizes to its content
        # (no more giant black bar with tiny crop centered).
        self.lbl_crop = QLabel("(no crop)")
        self.lbl_crop.setMinimumHeight(56)
        self.lbl_crop.setStyleSheet(
            f"background-color: #000; color: {DIM}; "
            "border: 1px solid #444; padding: 2px;"
        )
        self.lbl_crop.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_crop.setSizePolicy(
            self.lbl_crop.sizePolicy().horizontalPolicy(),
            self.lbl_crop.sizePolicy().verticalPolicy(),
        )
        lay.addWidget(self.lbl_crop)

        # Stretch spacer pushes the label + verdict to the right
        # only when there's slack — keeps the crop adjacent to its
        # value-label so the eye can compare them side-by-side.
        lay.addStretch(1)

        # User label (typed by the user via the Capture Labeler).
        # Big monospace so it visually anchors what the crop should match.
        self.lbl_label = QLabel("—")
        font = QFont("Consolas, monospace")
        font.setPointSize(18)
        font.setBold(True)
        self.lbl_label.setFont(font)
        self.lbl_label.setStyleSheet(f"color: {ACCENT};")
        self.lbl_label.setMinimumWidth(140)
        self.lbl_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl_label)

        # Verdict button.
        self.btn_verdict = QPushButton("PENDING")
        self.btn_verdict.setFixedSize(110, 32)
        self.btn_verdict.clicked.connect(self._cycle_verdict)
        lay.addWidget(self.btn_verdict)

        self._apply_verdict_style()

    def _apply_verdict_style(self) -> None:
        if self.verdict == "approved":
            self.btn_verdict.setText("APPROVED")
            self.btn_verdict.setStyleSheet(
                f"background-color: {ACCENT}; color: {BG}; "
                "font-weight: 700; border-radius: 4px;"
            )
            self.setStyleSheet(f"background-color: rgba(51,221,136,32);")
        elif self.verdict == "rejected":
            self.btn_verdict.setText("REJECTED")
            self.btn_verdict.setStyleSheet(
                f"background-color: {RED}; color: white; "
                "font-weight: 700; border-radius: 4px;"
            )
            self.setStyleSheet(f"background-color: rgba(255,68,68,32);")
        else:
            self.btn_verdict.setText("PENDING")
            self.btn_verdict.setStyleSheet(
                f"background-color: #2a2a2a; color: {DIM}; "
                "font-weight: 600; border-radius: 4px;"
            )
            self.setStyleSheet(f"background-color: {BG};")

    def _cycle_verdict(self) -> None:
        order = ["pending", "approved", "rejected"]
        i = order.index(self.verdict)
        self.verdict = order[(i + 1) % 3]
        self._apply_verdict_style()

    def set_verdict(self, v: str) -> None:
        if v not in ("approved", "rejected", "pending"):
            v = "pending"
        self.verdict = v
        self._apply_verdict_style()

    def update_content(
        self,
        crop: Optional[Image.Image],
        boxes: list[tuple[int, int, int, int]],
        label_text: str,
        verdict: str,
    ) -> None:
        if crop is None:
            self.lbl_crop.clear()
            self.lbl_crop.setText("(no crop)")
        else:
            # Render with per-glyph segmenter boxes overlaid + the
            # expected character labels above each box (same visual
            # convention as the signature row reviewer). Boxes that
            # disagree with the expected count signal a segmenter
            # failure even before we look at the digits themselves.
            scaled = _render_crop_with_glyph_boxes(
                crop, boxes, label_text or "", scale=3,
            )
            pix = QPixmap.fromImage(ImageQt(scaled))
            self.lbl_crop.setPixmap(pix)

        # Glyph-count badge. Expected = visible-glyph chars in the
        # user-typed label (commas stripped; trailing % stripped).
        # Actual = number of bboxes the production segmenter found.
        expected = _expected_segments(label_text)
        n_seg = len(boxes)
        n_exp = len(expected)
        if crop is None:
            self.lbl_count.setText("—")
            self.lbl_count.setStyleSheet(
                f"color: {DIM}; font-family: Consolas, monospace; "
                "font-size: 12px; font-weight: 700;"
            )
        elif n_exp == 0:
            # No usable label to compare against — show segmented
            # count only, in neutral colour.
            self.lbl_count.setText(f"{n_seg} segs")
            self.lbl_count.setStyleSheet(
                f"color: {DIM}; font-family: Consolas, monospace; "
                "font-size: 12px; font-weight: 700;"
            )
        elif n_seg == n_exp:
            self.lbl_count.setText(f"{n_seg}/{n_exp} ✓")
            self.lbl_count.setStyleSheet(
                f"color: {ACCENT}; font-family: Consolas, monospace; "
                "font-size: 13px; font-weight: 700;"
            )
        else:
            self.lbl_count.setText(f"{n_seg}/{n_exp} ✗")
            self.lbl_count.setStyleSheet(
                f"color: {RED}; font-family: Consolas, monospace; "
                "font-size: 13px; font-weight: 700;"
            )

        self.lbl_label.setText(label_text or "—")
        self.set_verdict(verdict)


class _Reviewer(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("HUD Row Reviewer")
        self.setStyleSheet(f"background-color: {BG}; color: {FG};")
        # Default size tuned to fit a 1366×768 laptop screen with
        # room for the user-typed label column on the right. Resizable.
        self.resize(900, 720)

        self.sidecars = _find_all_sidecars()
        self.idx = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # Header.
        header = QHBoxLayout()
        self.lbl_progress = QLabel("0 / 0")
        self.lbl_progress.setStyleSheet(
            f"color: {DIM}; font-family: Consolas, monospace;"
        )
        header.addWidget(self.lbl_progress)
        header.addStretch(1)
        self.lbl_path = QLabel("(no captures)")
        self.lbl_path.setStyleSheet(
            f"color: {DIM}; font-family: Consolas, monospace; font-size: 11px;"
        )
        header.addWidget(self.lbl_path)
        outer.addLayout(header)

        # Source panel preview (top half).
        self.lbl_panel = QLabel("(no image)")
        self.lbl_panel.setMinimumHeight(360)
        self.lbl_panel.setAlignment(Qt.AlignCenter)
        self.lbl_panel.setStyleSheet(
            f"background-color: #000; border: 1px solid #444;"
        )
        outer.addWidget(self.lbl_panel, 2)

        # Three field rows (bottom half).
        self.field_rows: dict[str, _FieldRow] = {}
        for f in FIELDS:
            row = _FieldRow(f, parent=self)
            outer.addWidget(row)
            self.field_rows[f] = row

        # Footer with buttons.
        footer = QHBoxLayout()
        btn_prev = QPushButton("◀ Prev (PgUp)")
        btn_prev.clicked.connect(self._go_prev)
        footer.addWidget(btn_prev)
        btn_skip = QPushButton("Skip ▶ (PgDn)")
        btn_skip.clicked.connect(self._go_next_no_save)
        footer.addWidget(btn_skip)
        footer.addStretch(1)
        btn_save = QPushButton("Save + Next (Ctrl+Enter)")
        btn_save.setStyleSheet(
            f"background-color: {ACCENT}; color: {BG}; "
            "font-weight: 700; padding: 6px 12px; border-radius: 4px;"
        )
        btn_save.clicked.connect(self._save_and_next)
        footer.addWidget(btn_save)
        outer.addLayout(footer)

        # Keyboard shortcuts.
        QShortcut(QKeySequence("PgDown"), self).activated.connect(self._save_and_next)
        QShortcut(QKeySequence("PgUp"), self).activated.connect(self._go_prev)
        QShortcut(QKeySequence("S"), self).activated.connect(self._go_next_no_save)
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self._save_and_next)
        QShortcut(QKeySequence("Ctrl+Enter"), self).activated.connect(self._save_and_next)
        QShortcut(QKeySequence("A"), self).activated.connect(self._approve_all)
        QShortcut(QKeySequence("R"), self).activated.connect(self._reject_all)
        QShortcut(QKeySequence("1"), self).activated.connect(
            lambda: self.field_rows["mass"]._cycle_verdict()
        )
        QShortcut(QKeySequence("2"), self).activated.connect(
            lambda: self.field_rows["resistance"]._cycle_verdict()
        )
        QShortcut(QKeySequence("3"), self).activated.connect(
            lambda: self.field_rows["instability"]._cycle_verdict()
        )
        QShortcut(QKeySequence("Escape"), self).activated.connect(QApplication.quit)

        self._refresh()

    # ── helpers ──

    def _approve_all(self) -> None:
        for row in self.field_rows.values():
            row.set_verdict("approved")

    def _reject_all(self) -> None:
        for row in self.field_rows.values():
            row.set_verdict("rejected")

    def _go_prev(self) -> None:
        if self.idx > 0:
            self.idx -= 1
            self._refresh()

    def _go_next_no_save(self) -> None:
        if self.idx + 1 < len(self.sidecars):
            self.idx += 1
            self._refresh()

    def _save_and_next(self) -> None:
        # Persist current verdict per field. Any field still in
        # "pending" state at save time is treated as APPROVED — the
        # user reviewed the capture, didn't reject anything, so the
        # remaining fields are good. Without this, Save+Next on a
        # capture where you didn't touch the verdict buttons would
        # save everything as "pending" (the default) which is
        # indistinguishable from "not yet reviewed". Matches the
        # signature row reviewer's "click-through = approve" UX.
        if 0 <= self.idx < len(self.sidecars):
            sc = self.sidecars[self.idx]
            doc = _load_sidecar(sc) or {}
            for f, row in self.field_rows.items():
                v = row.verdict
                if v == "pending":
                    v = "approved"
                doc[f"review_status_{f}"] = v
            _save_sidecar(sc, doc)
        # Advance.
        if self.idx + 1 < len(self.sidecars):
            self.idx += 1
            self._refresh()
        else:
            self.lbl_path.setText("(end of queue — saved)")

    def _refresh(self) -> None:
        if not self.sidecars:
            self.lbl_progress.setText("0 / 0")
            self.lbl_path.setText("(no labeled captures found)")
            return
        sc = self.sidecars[self.idx]
        png = sc.with_suffix(".png")
        doc = _load_sidecar(sc) or {}
        self.lbl_progress.setText(
            f"{self.idx + 1} / {len(self.sidecars)}"
        )
        self.lbl_path.setText(f"...{sc.parent.parent.name}/{sc.parent.name}/{sc.name}")

        # Render the panel preview.
        try:
            img = Image.open(png).convert("RGB")
        except Exception:
            self.lbl_panel.setText("(open failed)")
            return
        # Fit to display.
        max_h = 360
        if img.height > max_h:
            ratio = max_h / img.height
            preview = img.resize(
                (int(img.width * ratio), max_h), Image.LANCZOS,
            )
        else:
            scale = max(1, 360 // max(1, img.height))
            preview = img.resize(
                (img.width * scale, img.height * scale), Image.NEAREST,
            )
        self.lbl_panel.setPixmap(QPixmap.fromImage(ImageQt(preview)))

        # Extract per-field crops + per-glyph boxes via production
        # pipeline, populate rows.
        crops = _extract_value_crops(img)
        for f in FIELDS:
            label_text = _norm_label(doc.get(f))
            verdict = doc.get(f"review_status_{f}", "pending")
            crop, boxes = crops.get(f, (None, []))
            self.field_rows[f].update_content(
                crop, boxes, label_text, verdict,
            )


def main() -> int:
    app = QApplication(sys.argv)
    w = _Reviewer()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
