"""Simulator: would outlier rejection lift the lock rate?

Without modifying production code, replays _cold_start's anchor
collection on every captured panel and applies a synthetic
outlier-rejection step:

  1) Collect anchors as today.
  2) Solve LSQ with ALL anchors. If residuals pass -> LOCK (baseline).
  3) Otherwise, if we have >=4 anchors: drop the anchor with the
     largest residual, re-solve with the rest. If residuals pass
     and we still have >= _min_anchors_for_lock anchors -> LOCK
     (outlier-rejected).
  4) Otherwise -> NOT_LOCKED.

Reports per-tier (ANNOTATED / UNREVIEWED / SKIP) for:
  - baseline lock rate (= today's behavior)
  - outlier-rejected lock rate
  - which anchor was dropped most often (diagnostic)
"""
from __future__ import annotations

import glob
import logging
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from ocr.sc_ocr.hud_panel_tracker import (  # noqa: E402
    DEFAULT_OFFSETS,
    HudPanelTracker,
)

logging.getLogger().setLevel(logging.CRITICAL)


def _annotation_status(p: str) -> str:
    base = p[:-4]
    if os.path.exists(base + ".skip"):
        return "SKIP"
    if os.path.exists(base + ".boxes.json"):
        return "ANNOTATED"
    return "UNREVIEWED"


def _try_lock(
    tracker: HudPanelTracker,
    measurements: dict,
) -> tuple[bool, dict]:
    """Return (locked, residuals)."""
    if len(measurements) < tracker._min_anchors_for_lock:
        return False, {}
    pose, residuals = tracker._solve(measurements)
    return tracker._residuals_ok(residuals), residuals


def _try_lock_with_outlier_reject(
    tracker: HudPanelTracker,
    measurements: dict,
    max_drops: int = 1,
) -> tuple[bool, list[str]]:
    """Try baseline lock; if it fails, iteratively drop the worst-
    residual anchor up to `max_drops` times.

    Returns (locked, list_of_dropped_anchor_keys).
    """
    dropped: list[str] = []
    current = dict(measurements)
    for _ in range(max_drops + 1):
        ok, residuals = _try_lock(tracker, current)
        if ok:
            return True, dropped
        # Either too few anchors or residuals too high.
        if len(current) <= tracker._min_anchors_for_lock:
            return False, dropped
        if not residuals:
            return False, dropped
        # Drop worst.
        worst_key = max(residuals.items(), key=lambda kv: kv[1])[0]
        dropped.append(worst_key)
        current.pop(worst_key, None)
    return False, dropped


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    images = sorted(glob.glob(pat))
    print(f"found {len(images)} captures\n")
    if not images:
        return 1

    # tier -> counters
    base_locks: dict[str, int] = {"ANNOTATED": 0, "UNREVIEWED": 0, "SKIP": 0}
    or_locks: dict[str, int] = {"ANNOTATED": 0, "UNREVIEWED": 0, "SKIP": 0}
    tot: dict[str, int] = {"ANNOTATED": 0, "UNREVIEWED": 0, "SKIP": 0}
    saved_by_drop: dict[str, Counter[str]] = {
        "ANNOTATED": Counter(), "UNREVIEWED": Counter(), "SKIP": Counter(),
    }
    regressions: list[str] = []  # baseline-locked but OR-failed (shouldn't happen)

    for i, img_path in enumerate(images):
        tier = _annotation_status(img_path)
        tot[tier] += 1
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            measurements = t._collect_anchors(img, search_centers=None)
        except Exception:
            continue

        base_ok, _ = _try_lock(t, measurements)
        if base_ok:
            base_locks[tier] += 1
        or_ok, dropped = _try_lock_with_outlier_reject(t, measurements, max_drops=2)
        if or_ok:
            or_locks[tier] += 1
            if not base_ok and dropped:
                saved_by_drop[tier][dropped[0]] += 1
        if base_ok and not or_ok:
            # OR should never lose a lock — if it would, our logic is wrong.
            regressions.append(f"{tier}: {os.path.basename(img_path)}")

        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(images)}  "
                  f"ann base={base_locks['ANNOTATED']} OR={or_locks['ANNOTATED']}")

    print()
    print("=== Lock-rate comparison ===")
    print(f"{'tier':<12} {'total':>6} {'baseline':>10} {'+OR':>8} {'delta':>8}")
    for tier in ("ANNOTATED", "UNREVIEWED", "SKIP"):
        n = tot[tier]
        b = base_locks[tier]
        o = or_locks[tier]
        b_pct = (b * 100.0 / n) if n else 0.0
        o_pct = (o * 100.0 / n) if n else 0.0
        print(f"{tier:<12} {n:>6} {b:>5}({b_pct:4.1f}%) {o:>5}({o_pct:4.1f}%) {o-b:>+8}")

    print("\n=== Anchor dropped to save the lock (per tier) ===")
    for tier in ("ANNOTATED", "UNREVIEWED", "SKIP"):
        if not saved_by_drop[tier]:
            continue
        print(f"  {tier}:")
        for k, v in saved_by_drop[tier].most_common():
            print(f"    dropped {k:35s} {v} save(s)")

    if regressions:
        print(f"\n!!! REGRESSIONS ({len(regressions)}): baseline locked but OR didn't")
        for r in regressions[:20]:
            print(f"  {r}")
    else:
        print("\nNo regressions: OR never loses a lock the baseline had.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
