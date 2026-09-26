"""Deterministic train / held-out split.

A confirmed capture is HELD OUT when a hash of (family, confirmed label)
falls in the bottom HELDOUT_PCT percent. Grouping by the label value — not
by capture id — keeps repeat scans of the same rock (identical value,
near-identical pixels) on ONE side of the split; splitting by id would leak
near-duplicates into the test set and flatter a freshly trained candidate
against the stock model that never saw them.

Held-out captures are never extracted into glyphs, never synthesised from
and never trained on (train.py re-asserts this as a tripwire).
"""
from __future__ import annotations

import hashlib

HELDOUT_PCT = 20


def is_heldout(fam: str, label: str) -> bool:
    h = hashlib.sha256(f"{fam}|{label}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % 100 < HELDOUT_PCT


def split_of(fam: str, label: str | None) -> str | None:
    if label is None:
        return None
    return "heldout" if is_heldout(fam, label) else "train"
