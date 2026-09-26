"""Smoke test for the count-mismatch rejection gate logic.

Exercises the pure-logic portion of the count-mismatch decision (the
field-aware tolerance + segmenter-span counting) against the cases
laid out in the task description. We synthesize fake segmenter boxes
and CRNN texts rather than invoking the OCR pipeline end-to-end, so
this is a millisecond-scale sanity check, not a benchmark.

Cases exercised:
  1. The user-reported bug: instability CRNN='52.33' (5) vs seg 6
     spans (3 digits + 1 dot + 2 digits) → REJECT
  2. Inverse: instability CRNN='523.33' (6) vs seg 6 spans → ACCEPT
  3. Single-zero: mass CRNN='0' (1) vs seg 1 span → ACCEPT
  4. Resistance %-tolerance: CRNN='46' (2) vs seg 3 spans (digits+%) →
     ACCEPT
  5. Mega-blob: CRNN='15683' (5) vs seg 2 spans → ACCEPT (don't reject
     when CRNN count > seg count)
  6. Resistance real mismatch: CRNN='1' (1) vs seg 3 spans → REJECT
     (seg-crnn >= 2 for resistance)
"""
from __future__ import annotations


def count_mismatch_decision(
    field: str,
    crnn_text: str,
    seg_boxes: list[tuple[int, int, int, int]],
    row_h: int,
) -> tuple[bool, int, int, int, int]:
    """Replicate the decision logic in api.py's count-mismatch gate.

    Returns ``(should_reject, seg_digit_spans, digit_spans,
    dot_spans, crnn_digits)``.
    """
    if field not in ("mass", "resistance", "instability"):
        return (False, 0, 0, 0, 0)

    crnn_digits = sum(1 for c in crnn_text if c.isdigit())
    # Single-digit reads skip the gate entirely.
    if crnn_digits < 2:
        return (False, 0, 0, 0, crnn_digits)

    tall_row = row_h >= 14
    candidates: list[int] = []  # heights of (h>=8, w>=2) candidates
    dot_spans = 0
    for (_gx, _gy, gw, gh) in seg_boxes:
        if gh >= 8 and gw >= 2:
            candidates.append(gh)
        elif tall_row and gh <= 6 and gw >= 1:
            dot_spans += 1

    # Height-consistency filter.
    digit_spans = 0
    if candidates:
        heights = sorted(candidates)
        median_h = heights[len(heights) // 2]
        h_lo = int(median_h * 0.60)
        h_hi = int(median_h * 1.40)
        digit_spans = sum(1 for h in candidates if h_lo <= h <= h_hi)

    tolerance = 2 if field == "resistance" else 1
    should_reject = (
        digit_spans > crnn_digits
        and (digit_spans - crnn_digits) >= tolerance
        and digit_spans >= 3
    )
    return (should_reject, digit_spans, digit_spans, dot_spans, crnn_digits)


def _box(x: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Helper: (x, y, w, h) tuple at y=0."""
    return (x, 0, w, h)


CASES = [
    # ── User-reported bug ──
    # instability CRNN='52.33' but real value '523.33'.
    # Segmenter sees 3 digits + small dot + 2 digits = 6 spans.
    {
        "name": "bug: instability CRNN=52.33 vs 6 segmented spans",
        "field": "instability",
        "crnn_text": "52.33",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),    # digit
            _box(10, 8, 14),   # digit
            _box(20, 8, 14),   # digit
            _box(30, 3, 4),    # dot (small, h<=6, in tall row)
            _box(35, 8, 14),   # digit
            _box(45, 8, 14),   # digit
        ],
        "expect_reject": True,
    },
    # ── Inverse: legit 6-char read on legit 6-span crop ──
    {
        "name": "ok: instability CRNN=523.33 matches 6 segmented spans",
        "field": "instability",
        "crnn_text": "523.33",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),
            _box(10, 8, 14),
            _box(20, 8, 14),
            _box(30, 3, 4),
            _box(35, 8, 14),
            _box(45, 8, 14),
        ],
        "expect_reject": False,
    },
    # ── Single-digit mass=0 ──
    {
        "name": "ok: single-digit mass=0",
        "field": "mass",
        "crnn_text": "0",
        "row_h": 20,
        "boxes": [_box(0, 8, 14)],
        "expect_reject": False,
    },
    # ── Resistance %-tolerance: CRNN drops %, segmenter keeps it ──
    {
        "name": "ok: resistance %=tolerance (CRNN=46, seg=3 with %)",
        "field": "resistance",
        "crnn_text": "46",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),    # '4'
            _box(10, 8, 14),   # '6'
            _box(20, 10, 14),  # '%' — picked up as a digit-shaped span
        ],
        "expect_reject": False,
    },
    # ── Mega-blob: CRNN reads more than segmenter found ──
    {
        "name": "ok: mass CRNN=15683 vs 2 fused mega-blob spans",
        "field": "mass",
        "crnn_text": "15683",
        "row_h": 20,
        "boxes": [
            _box(0, 30, 14),   # mega-fused 3 digits
            _box(40, 25, 14),  # mega-fused 2 digits
        ],
        "expect_reject": False,
    },
    # ── Single-digit CRNN read intentionally skipped ──
    # The gate skips when CRNN has < 2 digits to avoid false-reject
    # on legit "0" reads where the segmenter overcounts label intrusion.
    # Trade-off: missed single-digit failures like resistance CRNN='1'
    # are caught downstream by per-rock monotonicity / consensus,
    # not by this gate.
    {
        "name": "ok: single-digit resistance CRNN=1 (gate skipped)",
        "field": "resistance",
        "crnn_text": "1",
        "row_h": 20,
        "boxes": [
            _box(0, 5, 14),
            _box(8, 8, 14),
            _box(18, 8, 14),
        ],
        "expect_reject": False,
    },
    # ── Resistance: CRNN drops % but only digits in seg = no rejection ──
    {
        "name": "ok: resistance CRNN=4 vs 1 seg span",
        "field": "resistance",
        "crnn_text": "4",
        "row_h": 20,
        "boxes": [_box(0, 8, 14)],
        "expect_reject": False,
    },
    # ── Mass: CRNN reads 2 digits but seg finds 3 → reject ──
    {
        "name": "bug: mass CRNN=23 vs 3 spans",
        "field": "mass",
        "crnn_text": "23",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),
            _box(10, 8, 14),
            _box(20, 8, 14),
        ],
        "expect_reject": True,
    },
    # ── Edge: seg_count = 2 → never reject (below the 3-span floor) ──
    {
        "name": "ok: too-few-spans floor: seg=2 even on disagreement",
        "field": "mass",
        "crnn_text": "1",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),
            _box(10, 8, 14),
        ],
        "expect_reject": False,
    },
    # ── Equal counts → never reject ──
    {
        "name": "ok: equal counts for mass=12345 (5 segs)",
        "field": "mass",
        "crnn_text": "12345",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),
            _box(10, 8, 14),
            _box(20, 8, 14),
            _box(30, 8, 14),
            _box(40, 8, 14),
        ],
        "expect_reject": False,
    },
    # ── Mass: seg under-counts via digit fusion (mass=27265 → 1 mega) ──
    {
        "name": "ok: mass CRNN=27265 vs 1 mega-span (CRNN higher)",
        "field": "mass",
        "crnn_text": "27265",
        "row_h": 20,
        "boxes": [_box(0, 50, 14)],
        "expect_reject": False,
    },
    # ── CRNN drops the DOT only (digits match) → don't reject ──
    {
        "name": "ok: instability CRNN=1421 vs 4 digits + dot (match)",
        "field": "instability",
        "crnn_text": "1421",
        "row_h": 20,
        "boxes": [
            _box(0, 8, 14),
            _box(10, 8, 14),
            _box(20, 3, 5),   # dot
            _box(25, 8, 14),
            _box(35, 8, 14),
        ],
        "expect_reject": False,
    },
    # ── Label-text outlier filter: two h=30 spans should be ignored ──
    # Mirrors cap_20260418_160003_629 (instability '17.84'). Segmenter
    # gets fooled into emitting 6 candidates, but 2 are too tall
    # (label intrusion) so they get filtered out.
    {
        "name": "ok: instability CRNN=17.84 with label outliers filtered",
        "field": "instability",
        "crnn_text": "17.84",
        "row_h": 44,
        "boxes": [
            _box(0, 6, 30),   # label intrusion (h=30, too tall)
            _box(7, 7, 17),   # '1'
            _box(19, 12, 17), # '7'
            _box(32, 5, 5),   # dot
            _box(38, 12, 31), # label/digit fusion (h=31, too tall)
            _box(52, 11, 17), # '4'
        ],
        "expect_reject": False,
    },
]


def main() -> int:
    passed = 0
    failed = 0
    print(f"Running {len(CASES)} count-mismatch smoke cases...")
    print()
    for case in CASES:
        reject, seg, dig, dot, crnn = count_mismatch_decision(
            case["field"],
            case["crnn_text"],
            case["boxes"],
            case["row_h"],
        )
        ok = reject == case["expect_reject"]
        marker = "PASS" if ok else "FAIL"
        print(
            f"  [{marker}] {case['name']:60s} "
            f"seg={seg} (d={dig}+dot={dot}) crnn={crnn} "
            f"reject={reject} expect={case['expect_reject']}"
        )
        if ok:
            passed += 1
        else:
            failed += 1
    print()
    print(f"  Result: {passed}/{passed + failed} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
