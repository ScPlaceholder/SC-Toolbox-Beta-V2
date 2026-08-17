"""Per-glyph RGB crop extractor for the mining-HUD CNN.

Takes the whole-strip RGB value crops already produced by
``scripts/extract_hud_value_crops.py`` (under
``training_data_hud_crops/{mass,resistance,instability}/*.png``) and
segments each one into per-glyph 28×28 RGB tiles paired with their
label characters.

Output structure (mirrors ``training_data_user_panel/`` but RGB):

    training_data_user_panel_rgb/
        0/  user_<ts>__cap_<ts>__<idx>.png
        1/
        ...
        9/
        dot/
        pct/

Each tile is 28×28 RGB, DARK-on-LIGHT-padded (matches the grayscale
HUD CNN's convention so the two trainers share a polarity).

**HUD/Signature font isolation** — the only allowed input sources
are HUD-registered staging directories. The script asserts
``training_registry.assert_path_belongs_to("hud", source_path)`` for
every file it touches, so a misconfigured cwd / wrong source dir
raises ``RegistryError`` instead of silently mixing fonts. The output
dir is also HUD-registered (``training_data_user_panel_rgb``).

The signature CNN and HUD CNN share no training data — by design.

Run::

    %LOCALAPPDATA%\\Python\\pythoncore-3.14-64\\python.exe \\
        scripts/extract_hud_glyph_crops_rgb.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent

if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

from ocr.training_registry import (  # noqa: E402
    assert_path_belongs_to,
    get as _registry_get,
    RegistryError,
)
from ocr.sc_ocr.api import (         # noqa: E402
    _segment_glyphs,
    _canonicalize_polarity,
    _adaptive_binarize,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
)
log = logging.getLogger("extract_hud_glyph_crops_rgb")

REGION_KIND = "hud"
SPEC = _registry_get(REGION_KIND)

# Filesystem-safe folder names that the trainer expects on disk.
LABEL_TO_FOLDER = {
    "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
    "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
    ".": "dot",
    "%": "pct",
}

# Source: whichever HUD crop staging dir exists. Both the dev tree and
# the WingmanAI install are listed in the registry's HUD spec.
_HUD_CROP_DIRS_TRY = [
    TOOL / "training_data_hud_crops",
    Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_hud_crops"
    ),
]

# Output is HUD-registered (training_registry adds
# ``training_data_user_panel_rgb`` to the HUD spec's sources). Pin it to
# the dev tree so subsequent ``assert_path_belongs_to`` calls succeed
# both inside the trainer and the extractor.
OUT_ROOT = TOOL / "training_data_user_panel_rgb"


# Pad colour (white) used in the grayscale segmenter — match it here so
# the RGB tiles' bg pixels look like the grayscale samples' bg pixels
# (~255). The actual ink colour is preserved as-is in RGB.
_PAD_RGB = (255, 255, 255)


def _label_to_chars(field: str, raw_label: str) -> list[str]:
    """Turn a stored label like ``"1p43"`` / ``"47"`` / ``"0"`` into the
    sequence of label characters the per-glyph crops will be tagged
    with. Trailing ``%`` is dropped for resistance because the
    whole-strip extractor strips it from the label (see
    ``scripts/extract_hud_value_crops._norm_label``)."""
    # Decode the filesystem-safe encoding: ``p`` → ``.``, ``n`` → ``-``.
    label = raw_label.replace("p", ".").replace("n", "-")
    # Numeric guard.
    try:
        v = float(label)
    except ValueError:
        return []
    if v == int(v):
        canon = str(int(v))
    else:
        canon = f"{v:.2f}"
    # All chars must be in the HUD label set; refuse anything else.
    out: list[str] = []
    for c in canon:
        if c not in SPEC.label_set:
            return []
        out.append(c)
    return out


def _segment_rgb(
    rgb_img: Image.Image,
) -> tuple[list[Image.Image], list[tuple[int, int, int, int]]]:
    """Run the production segmenter against an RGB crop, return RGB
    per-glyph 28×28 tiles + bboxes.

    Internally:
      1. Convert RGB → L for the segmenter's grayscale + binary path.
      2. Canonicalize polarity (the runtime does this before
         binarization so the segmenter's projection accumulator gets
         the right "ink direction" — see ocr/sc_ocr/api.py line ~7360).
      3. Adaptive-binarize and call ``_segment_glyphs`` to get the
         span list — but we don't keep its grayscale crops. Instead
         we re-crop the SAME bboxes from the ORIGINAL RGB image and
         pad them to 28×28 with white. The output preserves chromatic
         information that the grayscale path collapses.

    Returns the same ``(crops_28x28_rgb, source_bboxes)`` pair the
    grayscale segmenter does, with one extra invariant: the
    grayscale segmenter pads with 255 and the polarity-canonicalized
    grayscale; the RGB version pads with (255, 255, 255) on the
    ORIGINAL RGB. The runtime grayscale CNN was trained on the same
    convention. The RGB CNN will be trained against this convention.
    """
    rgb = np.array(rgb_img.convert("RGB"), dtype=np.uint8)
    gray = np.array(rgb_img.convert("L"), dtype=np.uint8)
    gray_canon = _canonicalize_polarity(gray)
    bin_pri = _adaptive_binarize(gray_canon)
    # _segment_glyphs returns grayscale crops + bboxes. We only need
    # the bboxes; ignore the gray crops it returns.
    _, boxes = _segment_glyphs(gray_canon, bin_pri)

    H, W = rgb.shape[:2]
    out_imgs: list[Image.Image] = []
    out_boxes: list[tuple[int, int, int, int]] = []
    pad = 2
    for x, y, w, h in boxes:
        # Source-coordinate sub-rect from the ORIGINAL RGB image.
        sub = rgb[y:y + h, x:x + w]
        if sub.size == 0:
            continue
        # Pad with white (matching the grayscale segmenter's 255-pad)
        # to create some breathing room around the glyph before resize.
        padded = np.full(
            (sub.shape[0] + pad * 2, sub.shape[1] + pad * 2, 3),
            255, dtype=np.uint8,
        )
        padded[pad:pad + sub.shape[0], pad:pad + sub.shape[1]] = sub
        pil = Image.fromarray(padded, mode="RGB").resize(
            (28, 28), Image.BILINEAR,
        )
        out_imgs.append(pil)
        out_boxes.append((int(x), int(y), int(w), int(h)))
    return out_imgs, out_boxes


def _walk_hud_crops() -> list[tuple[Path, str, str]]:
    """Find every (path, field, label_stub) tuple under the HUD crop
    staging. Verifies each path is HUD-registered before returning.
    Returns ``[]`` if no HUD crop staging exists."""
    items: list[tuple[Path, str, str]] = []
    chosen_root: Optional[Path] = None
    for root in _HUD_CROP_DIRS_TRY:
        if root.is_dir() and any(root.iterdir()):
            chosen_root = root
            break
    if chosen_root is None:
        log.error(
            "no training_data_hud_crops/ found in either dev tree "
            "or WingmanAI install — run scripts/extract_hud_value_crops.py "
            "first."
        )
        return items
    log.info("source HUD crop root: %s", chosen_root)
    for field in ("mass", "resistance", "instability"):
        d = chosen_root / field
        if not d.is_dir():
            continue
        for png in sorted(d.glob("*.png")):
            try:
                assert_path_belongs_to(REGION_KIND, png)
            except RegistryError as exc:
                # This should be impossible given the registry config, but
                # keep the tripwire loud — silent skipping would break the
                # "HUD-only" guarantee.
                raise SystemExit(
                    f"HUD/Signature isolation tripwire fired: {png} "
                    f"is not HUD-registered ({exc})."
                )
            # filename: ``user_<USERTS>__cap_<TS>__<LABEL>.png``
            parts = png.stem.split("__")
            if len(parts) < 3:
                continue
            label_stub = parts[-1]
            items.append((png, field, label_stub))
    return items


def _ensure_out_tree() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    for folder in LABEL_TO_FOLDER.values():
        (OUT_ROOT / folder).mkdir(parents=True, exist_ok=True)
    # Tripwire: make sure the output dir is HUD-registered (the spec
    # in training_registry.py lists training_data_user_panel_rgb in the
    # HUD sources — if a future edit removed it, the assert here would
    # blow up at extractor startup rather than later in the trainer).
    assert_path_belongs_to(REGION_KIND, OUT_ROOT)


def main() -> int:
    log.info("=== extract_hud_glyph_crops_rgb ===")
    log.info("region kind: %s", REGION_KIND)
    log.info("label set:   %r", SPEC.label_set)
    log.info("output root: %s", OUT_ROOT)
    _ensure_out_tree()

    items = _walk_hud_crops()
    if not items:
        return 1
    log.info("found %d source HUD crops", len(items))

    per_class_counts = {ch: 0 for ch in SPEC.label_set}
    n_skipped_label_mismatch = 0
    n_skipped_no_glyphs = 0
    n_skipped_bad_label = 0
    n_done = 0

    for i, (png, field, label_stub) in enumerate(items, start=1):
        if i % 50 == 0 or i == len(items):
            log.info("[%d/%d] %s/%s", i, len(items), field, png.name)
        chars = _label_to_chars(field, label_stub)
        if not chars:
            n_skipped_bad_label += 1
            continue
        # Resistance labels are stored as the integer percentage (e.g.
        # "47"), but the rendered HUD strip shows "47%" so the segmenter
        # produces one extra glyph for "%". For instability the label
        # already contains the decimal point (e.g. "1.43"). For mass
        # it's all digits.
        expected_chars = list(chars)
        if field == "resistance":
            # The on-screen strip includes a trailing %; allow either
            # ``chars`` or ``chars + '%'`` as the segmenter result.
            with_pct = chars + ["%"]
        else:
            with_pct = None

        try:
            tiles, boxes = _segment_rgb(Image.open(png))
        except Exception as exc:
            log.debug("  segment failed for %s: %s", png.name, exc)
            n_skipped_no_glyphs += 1
            continue
        if not tiles:
            n_skipped_no_glyphs += 1
            continue

        # Match the glyph count to the expected label length. Resistance
        # also accepts an extra trailing % glyph.
        if len(tiles) == len(expected_chars):
            assigned_chars = expected_chars
        elif (
            with_pct is not None
            and len(tiles) == len(with_pct)
        ):
            assigned_chars = with_pct
        else:
            # Glyph count doesn't match label length. Skip rather than
            # introduce mis-aligned (image, label) pairs.
            n_skipped_label_mismatch += 1
            continue

        # Save each glyph into its class folder.
        for idx, (tile, ch) in enumerate(zip(tiles, assigned_chars)):
            folder = LABEL_TO_FOLDER.get(ch)
            if folder is None:
                continue
            out_name = f"{png.stem}__{idx:02d}.png"
            out_path = OUT_ROOT / folder / out_name
            try:
                tile.save(out_path)
            except Exception as exc:
                log.debug("  save failed: %s: %s", out_path, exc)
                continue
            per_class_counts[ch] += 1
        n_done += 1

    log.info("=" * 50)
    log.info("Extraction complete:")
    log.info("  processed:                       %d", n_done)
    log.info("  skipped (bad label):             %d", n_skipped_bad_label)
    log.info("  skipped (no segments):           %d", n_skipped_no_glyphs)
    log.info("  skipped (glyph-count mismatch):  %d", n_skipped_label_mismatch)
    log.info("Per-class counts:")
    for ch in SPEC.label_set:
        log.info("  %r: %d", ch, per_class_counts[ch])

    # Soft warning if any class is below the floor — the trainer is the
    # final gate, but flag it here so the user knows.
    floor = SPEC.floor_per_class
    short = [
        ch for ch in SPEC.label_set
        if per_class_counts[ch] < floor
    ]
    if short:
        log.warning(
            "classes below floor (%d): %s — trainer will refuse "
            "until these have enough samples.",
            floor, sorted(short),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
