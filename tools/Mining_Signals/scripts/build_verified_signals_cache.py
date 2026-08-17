"""Build ``.mining_signals_verified.json`` from approved Glyph Forge
sidecars.

The signature recognizer's lexicon is built by multiplier-expanding
each ``scanSignature`` base in the mining chart (``base * n`` for
``n in 1..25``). That covers most chart-listed signatures but
empirically misses combinations the chart-export pipeline never
emits — values like 10000, 26000, 14000, 21565, 8276, 2000 that the
user has manually labeled via Glyph Forge + Row Reviewer but the
chart doesn't list.

When the CRNN reads one of those missing values correctly at high
confidence, the lexicon-gate rejects it as "off-chart" and falls
through to the broken legacy stack. Closing the lexicon gap with a
union of user-verified values fixes the rejection without
introducing any new pipeline machinery.

This script walks both the SC_Toolbox dev tree and the WingmanAI
distribution tree for ``*.glyphs.json`` sidecars, takes those with
``review_status == "approved"``, extracts each one's ``label`` field
(e.g. ``"10,000"``), strips non-digits, and writes the unique integer
set to ``.mining_signals_verified.json`` at the tree root.

The file's schema is intentionally minimal::

    {
        "schema": "verified_signals_v1",
        "values": [2000, 8276, 10000, ...]
    }

Loaded at api.py boot via ``_load_verified_signal_values()``.

Run:
    python scripts/build_verified_signals_cache.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Both tree roots contribute approved sidecars — the WingmanAI tree
# is where Glyph Forge typically writes (it's the live runtime
# install for ShipBit users), the SC_Toolbox tree may also have
# them when the user runs the dev runtime directly.
TREE_ROOTS = [
    Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
        r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    ),
    Path(__file__).resolve().parent.parent,
]


def collect_verified_values() -> set[int]:
    """Walk all known capture trees, return the union of approved
    sidecar labels parsed to integers."""
    verified: set[int] = set()
    n_seen = 0
    n_approved = 0
    n_with_label = 0
    for root in TREE_ROOTS:
        if not root.exists():
            print(f"  (skip: {root} not present)")
            continue
        captures_root = root / "training_data_panels"
        if not captures_root.exists():
            print(f"  (skip: {captures_root} not present)")
            continue
        for sc in captures_root.rglob("*.glyphs.json"):
            n_seen += 1
            try:
                data = json.loads(sc.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("review_status") != "approved":
                continue
            n_approved += 1
            label = (data.get("label") or "").strip()
            digits = "".join(c for c in label if c.isdigit())
            if not digits:
                continue
            try:
                verified.add(int(digits))
                n_with_label += 1
            except ValueError:
                pass
    print(
        f"  scanned {n_seen} sidecars  "
        f"approved={n_approved}  with-label={n_with_label}"
    )
    return verified


def main() -> int:
    verified = collect_verified_values()
    if not verified:
        print("FATAL: no verified signatures found — nothing to write")
        return 1
    out_payload = {
        "schema": "verified_signals_v1",
        "values": sorted(verified),
    }

    # Write to BOTH tree roots so production reads the same data
    # regardless of which install is in front.
    written = 0
    for root in TREE_ROOTS:
        if not root.exists():
            continue
        out_path = root / ".mining_signals_verified.json"
        try:
            out_path.write_text(
                json.dumps(out_payload, indent=2), encoding="utf-8",
            )
            print(f"  wrote {len(verified)} values to {out_path}")
            written += 1
        except Exception as exc:
            print(f"  WRITE FAILED at {out_path}: {exc}")
    if not written:
        print("FATAL: could not write to any tree root")
        return 1
    print(f"\n  unique verified values: {sorted(verified)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
