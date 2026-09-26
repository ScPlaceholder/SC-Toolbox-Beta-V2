"""Signature reader accuracy gate — the ~233 human-labeled region2
panels (review-approved) are THE accuracy gate for
``api._signal_recognize_pil``.

Scores the production reader per panel vs the GT value in ``cap_*.json``,
with the known-value lexicon loaded (runtime-faithful) and the consensus
buffers reset per panel (each judged fresh — this is the single-frame
gate; it cannot see live multi-frame voting).

Baseline (June 2026, pre-0/5-snap): 85.8% (200/233).

Run: python scripts/signature_accuracy_eval.py
"""
import platform  # noqa
platform._wmi = None
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image  # noqa: E402
from ocr.sc_ocr import api  # noqa: E402


def _find_root() -> Path:
    """Locate the LARGEST value-labeled region2 panel set (the 233 gate
    lives in the roaming tree; the Documents copy is a partial subset)."""
    cands = set()
    for base in [Path.home() / "AppData" / "Roaming", Path.home() / "Documents",
                 Path(__file__).resolve().parents[1]]:
        try:
            cands.update(base.glob("**/training_data_panels"))
        except Exception:
            pass
    best, best_n = None, 0
    for r in cands:
        n = 0
        for ud in r.glob("user_*"):
            r2 = ud / "region2"
            if r2.is_dir():
                n += sum(1 for jp in r2.glob("*.json")
                         if jp.with_suffix(".png").exists())
        if n > best_n:
            best, best_n = r, n
    return best


def _load_lexicon() -> int:
    vals = set()
    root = Path(__file__).resolve().parents[1]
    for p in [root / ".signals_cache.json", root / ".mining_chart_cache.json"]:
        if not p.exists():
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue

        def walk(x):
            if isinstance(x, bool):
                return
            if isinstance(x, (int, float)) and 1000 <= x <= 999999:
                vals.add(int(x))
            elif isinstance(x, str) and x.replace(",", "").isdigit() \
                    and 1000 <= int(x.replace(",", "")) <= 999999:
                vals.add(int(x.replace(",", "")))
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(d)
    api.set_known_signal_values(vals)
    return len(api._KNOWN_SIGNAL_VALUES)


def main() -> int:
    root = _find_root()
    n_lex = _load_lexicon()
    panels = []
    for ud in sorted(root.glob("user_*")):
        r2 = ud / "region2"
        if not r2.is_dir():
            continue
        for jp in sorted(r2.glob("*.json")):
            png = jp.with_suffix(".png")
            if not png.exists():
                continue
            try:
                m = json.load(open(jp))
            except Exception:
                continue
            v = m.get("value")
            if v and str(v).replace(",", "").isdigit():
                panels.append((png, int(str(v).replace(",", ""))))
    print("root: %s" % root)
    print("lexicon: %d known values; panels: %d\n" % (n_lex, len(panels)))
    correct = wrong = none = 0
    wrongs = []
    for i, (png, gt) in enumerate(panels, 1):
        try:
            api._reset_consensus_buffers()
        except Exception:
            pass
        try:
            img = Image.open(png).convert("RGB")
            rd = api._signal_recognize_pil(img, region=None)
        except Exception:
            rd = None
        if rd == gt:
            correct += 1
        elif rd is None:
            none += 1
        else:
            wrong += 1
            wrongs.append((png.name, gt, rd))
        if i % 50 == 0:
            print("  ...%d/%d  (correct=%d)" % (i, len(panels), correct))
    tot = len(panels)
    print("\nCORRECT: %d/%d (%.1f%%)" % (correct, tot, 100 * correct / max(1, tot)))
    print("WRONG:   %d" % wrong)
    print("NONE:    %d" % none)
    n05_gt = sum(1 for _, gt in panels if gt % 10 not in (0, 5))
    n05_wrong_label = sum(1 for _, gt, _ in wrongs if gt % 10 not in (0, 5))
    print("\n%d panels have non-0/5 GT (transient/poisoned labels); %d of the "
          "WRONG are vs such labels" % (n05_gt, n05_wrong_label))
    print("\nfirst 30 wrong:")
    for nm, gt, rd in wrongs[:30]:
        flag = "  <-- non-0/5 GT (poisoned)" if gt % 10 not in (0, 5) else ""
        print("  %-32s GT=%-7d read=%s%s" % (nm, gt, rd, flag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
