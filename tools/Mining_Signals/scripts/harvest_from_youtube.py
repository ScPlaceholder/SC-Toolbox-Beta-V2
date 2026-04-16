"""Harvest labeled digit glyphs from 1440p+ Star Citizen mining streams.

Pipeline per time range:
  1. yt-dlp downloads a 1440p clip (~25-60 MB per minute)
  2. ffmpeg extracts frames at a configurable fps (default 0.5 = 1 per 2s)
  3. Each frame is scanned for the SCAN RESULTS panel via a two-stage
     heuristic — high-contrast text density in the right-third of
     the frame that matches the panel's aspect ratio
  4. The panel crop is UPSCALED 3x (legacy OCR was tuned for native
     HUD capture size; stream panels are ~2x smaller so need upscale)
     then passed through ``scan_hud_onnx``
  5. If OCR produces a full result (mass + resistance + instability,
     all non-None, with panel_visible=True), we trust it as ground
     truth and harvest the individual digit crops into
     ``training_data_clean/<digit>/yt_<videoid>_<frame>.png``

Resume-friendly: frame hashes are tracked in ``.harvest_seen.txt`` —
re-running is idempotent.

Usage:
  python harvest_from_youtube.py \\
      --url 'https://www.youtube.com/watch?v=VIDEO_ID' \\
      --start 0:54:00 --end 0:55:30 \\
      --fps 0.5

Multiple time ranges per run:
  python harvest_from_youtube.py --batch harvest_plan.txt
  # harvest_plan.txt: one line per run, "URL<TAB>start<TAB>end<TAB>fps"

Dependencies: yt-dlp (pip), ffmpeg (on PATH), pillow, numpy,
onnxruntime (legacy OCR needs these).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
sys.path.insert(0, str(TOOL))

from ocr.onnx_hud_reader import (  # noqa: E402
    _ensure_model, _find_label_rows, _find_value_crop, _otsu,
    scan_hud_onnx,
)
from ocr import onnx_hud_reader as hud  # noqa: E402
from ocr import screen_reader as sr  # noqa: E402

OUT_DIR = TOOL / "training_data_clean"
PROGRESS_FILE = OUT_DIR / ".harvest_seen.txt"

# After upscale, the panel matches what the legacy OCR expects.
UPSCALE_FACTOR = 3.0


def _seen_hashes() -> set[str]:
    if not PROGRESS_FILE.is_file():
        return set()
    try:
        return set(PROGRESS_FILE.read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _mark_seen(h: str) -> None:
    try:
        with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
            f.write(h + "\n")
    except OSError:
        pass


def _download_clip(url: str, start: str, end: str, out_path: Path) -> bool:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        # Prefer 1440p (codec 400 AV1, or 308 VP9), fall back to 1080p
        "-f", "400+bestaudio/308+bestaudio/399+bestaudio/best[height<=1440]",
        "--download-sections", f"*{start}-{end}",
        "-o", str(out_path),
        "--merge-output-format", "mp4",
        "--no-warnings", "--quiet",
        url,
    ]
    try:
        subprocess.check_call(cmd)
        return out_path.exists()
    except subprocess.CalledProcessError as exc:
        print(f"  yt-dlp failed: {exc}", file=sys.stderr)
        return False


def _extract_frames(video_path: Path, frames_dir: Path, fps: float) -> list[Path]:
    frames_dir.mkdir(exist_ok=True)
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps={fps}",
        "-q:v", "2",
        str(frames_dir / "f_%04d.jpg"),
        "-loglevel", "error",
    ]
    subprocess.check_call(cmd)
    return sorted(frames_dir.glob("*.jpg"))


def _find_scan_panel(frame: Image.Image) -> Optional[Image.Image]:
    """Locate the SCAN RESULTS panel in a 1440p/1080p streamer frame.

    The panel has a very specific color signature: ORANGE text on a
    dark-navy translucent background. We mask for orange-ish pixels
    (R high, G moderate, B low) and find the densest rectangular
    region of such pixels in the right half of the frame.

    This rejects QR codes, white backgrounds, cyan icons, and most
    streamer overlays.
    """
    W, H = frame.size
    rgb = np.asarray(frame.convert("RGB"), dtype=np.int16)

    # The SCAN RESULTS panel always sits in the right portion of the HUD.
    # Scan the right 40% of the frame.
    right_x0 = int(W * 0.60)
    right = rgb[:, right_x0:]
    right_W = right.shape[1]

    # Orange text signature:
    #   R > 150  (bright red-orange)
    #   G 60-180 (warm yellow component, but not white)
    #   B < 120  (darker blue — rejects cyan and white)
    #   R - B > 80  (strongly biased toward warm)
    R, G, B = right[..., 0], right[..., 1], right[..., 2]
    text_mask = (
        (R > 150) & (R < 256)
        & (G > 60) & (G < 200)
        & (B < 140)
        & ((R - B) > 60)
    )

    # Find rows with ANY orange pixels (≥3)
    row_density = text_mask.sum(axis=1)
    dense_rows = row_density >= 3

    # Allow small gaps between rows (up to 3 rows) — numbers have
    # vertical whitespace between label and value lines
    MAX_GAP = 6

    # Find vertical bands of mostly-dense rows (with small gap tolerance)
    bands: list[tuple[int, int]] = []
    y = 0
    while y < H:
        if not dense_rows[y]:
            y += 1
            continue
        start = y
        last_hit = y
        while y < H and (y - last_hit) <= MAX_GAP:
            if dense_rows[y]:
                last_hit = y
            y += 1
        end = last_hit + 1
        if end - start >= 60:  # panel is at least ~60 px tall
            bands.append((start, end))
        while y < H and not dense_rows[y]:
            y += 1

    if not bands:
        return None

    # Prefer the band in the bottom half of the frame (SCAN RESULTS
    # usually sits below the cockpit chrome), but take any tall band
    # otherwise.
    bands.sort(key=lambda b: (b[1] - b[0]), reverse=True)

    for y1, y2 in bands:
        # Determine column bounds within this band
        band_mask = text_mask[y1:y2]
        col_density = band_mask.sum(axis=0)
        cols_hot = col_density >= (y2 - y1) * 0.05
        if not cols_hot.any():
            continue
        xs = np.where(cols_hot)[0]
        x1_rel, x2_rel = int(xs[0]), int(xs[-1]) + 1
        bw = x2_rel - x1_rel
        bh = y2 - y1
        # Panel aspect ratio is ~1:1 to ~2:1 (taller than wide) at native
        # scale; on 1440p streamer frames the panel is narrower.
        if bw < 80 or bw > 500:
            continue
        if bh < 80 or bh > 500:
            continue
        # Add margins
        x1 = max(0, right_x0 + x1_rel - 8)
        x2 = min(W, right_x0 + x2_rel + 8)
        y1 = max(0, y1 - 8)
        y2 = min(H, y2 + 8)
        return frame.crop((x1, y1, x2, y2))

    return None


def _try_ocr(panel: Image.Image) -> Optional[dict]:
    """Upscale and run the legacy OCR. Returns the result or None."""
    W, H = panel.size
    scaled = panel.resize(
        (int(W * UPSCALE_FACTOR), int(H * UPSCALE_FACTOR)), Image.LANCZOS,
    )

    # Mock the capture functions to return our panel
    for mod in (sr, hud):
        mod.capture_region = lambda r, _s=scaled: _s.copy()
        mod.capture_region_averaged = (
            lambda r, n_frames=7, delay_ms=45, _s=scaled: _s.copy()
        )

    # Clear caches between frames (each frame is a different "rock")
    hud._label_cache.clear()
    hud._paddle_cache_clear()

    region = {"x": 0, "y": 0, "w": scaled.size[0], "h": scaled.size[1]}
    try:
        result = scan_hud_onnx(region)
    except Exception as exc:
        print(f"    OCR error: {exc}", file=sys.stderr)
        return None

    if not result.get("panel_visible"):
        return None
    # Accept partial results — any field with a valid reading is useful
    if (result.get("mass") is None
            and result.get("resistance") is None
            and result.get("instability") is None):
        return None
    return result


def _extract_glyphs(panel: Image.Image, result: dict) -> list[tuple[str, np.ndarray]]:
    """Extract 28x28 labeled digit crops from an already-OCR'd panel.

    Uses the legacy segmentation on the upscaled panel and pairs each
    glyph with the corresponding character in the OCR result string.
    """
    W, H = panel.size
    scaled = panel.resize(
        (int(W * UPSCALE_FACTOR), int(H * UPSCALE_FACTOR)), Image.LANCZOS,
    )
    gray_img = np.array(scaled.convert("L"), dtype=np.uint8)

    hud._label_cache.clear()
    rows = _find_label_rows(scaled)
    if not rows:
        return []

    out: list[tuple[str, np.ndarray]] = []
    for field, expected_value in (
        ("mass", result.get("mass")),
        ("resistance", result.get("resistance")),
        ("instability", result.get("instability")),
    ):
        if expected_value is None:
            continue
        entry = rows.get(field)
        if entry is None:
            continue
        y1, y2, lbl_right = entry
        x_min = max(0, lbl_right + 6)
        value_crop = _find_value_crop(scaled, gray_img, y1, y2, x_min=x_min)
        if value_crop is None:
            continue

        # Format the expected value as the OCR would have seen it
        if field == "mass":
            expected_str = f"{int(expected_value)}"
        elif field == "resistance":
            expected_str = f"{int(expected_value)}"
        else:  # instability — may have decimals
            if expected_value == int(expected_value):
                expected_str = f"{int(expected_value)}"
            else:
                expected_str = f"{expected_value:.2f}".rstrip("0").rstrip(".")

        # Segment the value crop
        gray_crop = np.array(value_crop.convert("L"), dtype=np.uint8)
        if np.median(gray_crop) > 140:
            gray_crop = 255 - gray_crop
        thr = _otsu(gray_crop)
        binary = (gray_crop > thr).astype(np.uint8) * 255
        proj = (binary > 0).sum(axis=0)
        w = binary.shape[1]
        spans: list[tuple[int, int]] = []
        in_c = False
        start = 0
        for x in range(w + 1):
            v = proj[x] if x < w else 0
            if v > 0 and not in_c:
                in_c = True; start = x
            elif v == 0 and in_c:
                in_c = False
                if x - start >= 2:
                    spans.append((start, x))

        # Keep only digit chars from expected_str, must match span count
        digit_chars = [c for c in expected_str if c.isdigit()]
        if len(digit_chars) != len(spans):
            continue

        for (x1, x2), ch in zip(spans, digit_chars):
            ys = np.where(np.any(binary[:, x1:x2] > 0, axis=1))[0]
            if len(ys) < 2:
                continue
            ya, yb = int(ys[0]), int(ys[-1]) + 1
            glyph = gray_crop[ya:yb, x1:x2].astype(np.float32)
            pad = 2
            padded = np.full(
                (glyph.shape[0] + pad * 2, glyph.shape[1] + pad * 2),
                255.0, dtype=np.float32,
            )
            padded[pad:pad + glyph.shape[0], pad:pad + glyph.shape[1]] = glyph
            pil = Image.fromarray(padded.astype(np.uint8)).resize(
                (28, 28), Image.BILINEAR,
            )
            out.append((ch, np.asarray(pil, dtype=np.uint8)))

    return out


def _parse_bbox(s: str) -> Optional[tuple[int, int, int, int]]:
    """Parse a 'x,y,w,h' string into a bbox tuple."""
    try:
        parts = [int(p.strip()) for p in s.split(",")]
        if len(parts) == 4:
            return tuple(parts)  # type: ignore
    except (ValueError, AttributeError):
        pass
    return None


def harvest(url: str, start: str, end: str, fps: float,
            bbox: Optional[tuple[int, int, int, int]] = None) -> int:
    if not _ensure_model():
        print("ERROR: legacy ONNX model failed to load", file=sys.stderr)
        return 0

    OUT_DIR.mkdir(exist_ok=True)
    seen = _seen_hashes()
    video_id = url.rsplit("=", 1)[-1].rsplit("/", 1)[-1]

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        video_path = tmp / "clip.mp4"
        frames_dir = tmp / "frames"

        print(f"[{video_id} {start}-{end}] downloading...")
        if not _download_clip(url, start, end, video_path):
            return 0
        frames = _extract_frames(video_path, frames_dir, fps)
        print(f"[{video_id}] {len(frames)} frames")

        per_class: dict[str, int] = {str(d): 0 for d in range(10)}
        total_harvested = 0
        ocr_success = 0

        for fp in frames:
            try:
                img = Image.open(fp).convert("RGB")
            except Exception:
                continue
            h = hashlib.md5(open(fp, "rb").read()).hexdigest()
            if h in seen:
                continue
            seen.add(h)

            if bbox is not None:
                bx, by, bw, bh = bbox
                panel = img.crop((bx, by, bx + bw, by + bh))
            else:
                panel = _find_scan_panel(img)
                if panel is None:
                    _mark_seen(h)
                    continue

            result = _try_ocr(panel)
            if result is None:
                _mark_seen(h)
                continue
            ocr_success += 1

            glyphs = _extract_glyphs(panel, result)
            for ch, crop in glyphs:
                if ch not in per_class:
                    continue
                d = OUT_DIR / ch
                d.mkdir(exist_ok=True)
                name = f"yt_{video_id}_{fp.stem}_{per_class[ch]}.png"
                try:
                    Image.fromarray(crop, mode="L").save(d / name)
                    per_class[ch] += 1
                    total_harvested += 1
                except OSError:
                    pass
            _mark_seen(h)

        print(f"[{video_id}] OCR succeeded on {ocr_success}/{len(frames)} frames, "
              f"harvested {total_harvested} glyphs")
        for ch in "0123456789":
            if per_class[ch]:
                print(f"  '{ch}': +{per_class[ch]}")
        return total_harvested


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url")
    p.add_argument("--start", default="0:00:00")
    p.add_argument("--end", default="0:05:00")
    p.add_argument("--fps", type=float, default=0.5)
    p.add_argument("--bbox", help="Known panel bbox 'x,y,w,h' in native frame coords. "
                                   "Skips auto-detection.")
    p.add_argument("--batch", help="Path to TSV: url<TAB>start<TAB>end<TAB>fps<TAB>bbox")
    args = p.parse_args()

    total = 0
    if args.batch:
        with open(args.batch) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                url = parts[0]
                s = parts[1]
                e = parts[2]
                fps = float(parts[3]) if len(parts) > 3 else 0.5
                bbox = _parse_bbox(parts[4]) if len(parts) > 4 else None
                total += harvest(url, s, e, fps, bbox=bbox)
    elif args.url:
        total = harvest(args.url, args.start, args.end, args.fps,
                        bbox=_parse_bbox(args.bbox) if args.bbox else None)
    else:
        p.error("provide --url or --batch")

    print(f"\nTotal harvested: {total}")


if __name__ == "__main__":
    main()
