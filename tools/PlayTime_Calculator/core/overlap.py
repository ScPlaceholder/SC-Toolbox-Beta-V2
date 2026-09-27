"""Sessions that claim the same wall-clock time.

Requested 2026-09-26: "add a tracker to keep sessions from overlapping."

⛔ IT REPORTS. IT DOES NOT MERGE, TRIM OR DROP ANYTHING, and that is the whole design decision.
  An overlap between two log files means one of two things and this module cannot tell them apart:
    (a) two Star Citizen clients genuinely ran at once — LIVE and PTU are separate log trees, so
        this is possible, and both sessions are real play time
    (b) a bookkeeping fault — a log double-counted, a clock jump, a timestamp parsed wrong
  Silently deduplicating would be right for (b) and would DELETE REAL PLAY TIME in (a). A total
  that quietly shrank would look like the tracker working. So the honest output is a list, and the
  decision about what it means stays with whoever reads it.

⚠ WHAT IT FOUND WHEN IT WAS WRITTEN: nothing. Zero overlapping pairs across all 1,114 sessions in
  the owner's real cache. That is recorded here deliberately, because a detector nobody has ever
  seen return a row is indistinguishable from a broken one — which is why `selftest()` below
  constructs overlaps and requires them to be caught. A clean board is only meaningful from an
  instrument that has been shown to be capable of a dirty one.

⚠ AND "no overlaps today" IS NOT "overlaps are impossible". The channels really are independent.
  This exists so that the day it happens, it is a line in a report rather than a number nobody can
  explain.
"""
from __future__ import annotations


def find_overlaps(sessions) -> list:
    """-> [(a, b, seconds_of_overlap)], every pair whose local times intersect.

    Sorted by start, then each session compared forward only while a later start is still before
    THIS session's end. Near-linear rather than all-pairs, and that is not only speed: an
    all-pairs scan over 1,114 sessions is 620,000 comparisons, slow enough that it would have been
    run once and then quietly dropped from the refresh path.

    ⛔ THE FIRST VERSION OF THIS TRACKED A RUNNING `max_end` — the furthest end seen so far — and
      broke on `b.start >= max_end`. I wrote a confident comment explaining that it was needed so
      one 47-hour session could not swallow a dozen others. **That comment was wrong and the code
      was wrong with it.** `max_end` belongs to some OTHER session, so the loop both ran past the
      point where `a` could still overlap anything AND computed the intersection against a
      stranger's end. It did not find more pairs. It INVENTED pairs that do not exist:

          A 00:00-04:00   B 02:00-10:00   C 06:00-08:00
          correct : A-B and B-C          (A ended two hours before C began)
          max_end : A-B, B-C **and A-C** (measured against B's end, not A's)

    ★ AND MY SELFTEST PASSED BOTH VERSIONS. Mutating the break condition changed nothing the
      suite could see, because every case I had written put the long session FIRST, where the two
      forms agree. What caught it was a differential run: 4,000 random arrangements through both,
      2,839 disagreeing, with the first difference at trial 0. The test I trusted was the one that
      could not fail; the check that took thirty seconds was the one that could.
      `test_the_max_end_variant_invents_a_pair` below is that case, pinned.
    """
    ordered = sorted(sessions, key=lambda s: s.start_local)
    out = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.start_local >= a.end_local:
                break
            secs = (min(a.end_local, b.end_local) - b.start_local).total_seconds()
            if secs > 0:
                out.append((a, b, secs))
    return out


def report(sessions) -> dict:
    """-> {"checked", "pairs", "seconds", "worst"}.

    ⚠ `seconds` SUMS THE PAIRS AND IS NOT THE DOUBLE-COUNTED TIME. Three mutually overlapping
      sessions produce three pairs over one stretch of clock, so this number can exceed the real
      duplication. It is a magnitude to sort by, never a correction to apply to a total — named
      `seconds` rather than `duplicated` so nobody is tempted to subtract it.
    """
    sessions = list(sessions)
    pairs = find_overlaps(sessions)
    return {
        "checked": len(sessions),
        "pairs": len(pairs),
        "seconds": sum(p[2] for p in pairs),
        "worst": max(pairs, key=lambda p: p[2]) if pairs else None,
    }


def selftest() -> int:
    """Construct overlaps and require them to be caught. Run: python -m core.overlap

    ★ THIS IS THE POINT OF THE FILE. On the owner's real data this module returns zero, and a zero
      from an unexercised detector is worth nothing. Every case below is a POSITIVE control: it
      builds a known-dirty input and fails if the scan calls it clean.
    """
    from datetime import datetime, timedelta, timezone

    class _S:
        def __init__(self, name, start_h, hours):
            self.path = name
            self.channel = name
            base = datetime(2025, 1, 1, tzinfo=timezone.utc)
            self.start = base + timedelta(hours=start_h)
            self.end = self.start + timedelta(hours=hours)
            self.duration_seconds = hours * 3600.0

        @property
        def start_local(self):
            return self.start.astimezone()

        @property
        def end_local(self):
            return self.end.astimezone()

        def __repr__(self):
            return "<%s>" % self.path

    ok = True

    def ck(name, cond, detail=""):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))
        if not cond:
            ok = False

    a, b = _S("A", 0, 4), _S("B", 2, 4)
    r = report([a, b])
    ck("a plain overlap is found", r["pairs"] == 1, "got %d" % r["pairs"])
    ck("the overlap is measured, not just flagged",
       abs(r["seconds"] - 2 * 3600) < 1, "got %.0fs, expected 7200" % r["seconds"])

    # THE NEGATIVE CONTROL. Touching-but-not-overlapping must stay SILENT, or every adjacent pair
    # in a normal day is a finding and the tracker becomes noise nobody reads.
    c, d = _S("C", 0, 4), _S("D", 4, 4)
    ck("back-to-back sessions are NOT an overlap", report([c, d])["pairs"] == 0)

    # A long session really does pair with every session inside it.
    long_ = _S("LONG", 0, 48)
    shorts = [_S("s1", 1, 1), _S("s2", 10, 1), _S("s3", 40, 1)]
    r3 = report([long_] + shorts)
    ck("a long session pairs with EVERY session inside it",
       r3["pairs"] == 3, "got %d, expected 3" % r3["pairs"])

    ck("input order does not change the result",
       report(list(reversed([long_] + shorts)))["pairs"] == 3)

    # ⛔ THE CASE THAT DISCRIMINATES, and the only one in this file that would have caught the bug
    #   the first version shipped with. A(0-4) B(2-10) C(6-8): A ended two hours before C began,
    #   so the honest answer is two pairs. A scan carrying a running `max_end` measures C against
    #   B's end instead of A's and reports a third, entirely fictional overlap.
    #   Every OTHER case above passes under both implementations — which is exactly why a suite
    #   that is green tells you nothing until one of its cases can go red for the right reason.
    r7 = report([_S("A", 0, 4), _S("B", 2, 8), _S("C", 6, 2)])
    ck("a session is never paired with one that starts after IT ends",
       r7["pairs"] == 2, "got %d, expected 2 (A-B, B-C); 3 means A-C was invented" % r7["pairs"])

    ck("an empty list is clean, not an error", report([])["pairs"] == 0)
    ck("a single session cannot overlap itself", report([a])["pairs"] == 0)

    r6 = report([_S("x", 0, 10), _S("y", 9, 2), _S("z", 1, 8)])
    ck("worst names the biggest overlap, not the earliest",
       r6["worst"] is not None and r6["worst"][2] > 7 * 3600,
       "worst=%.0fs" % (r6["worst"][2] if r6["worst"] else -1))

    print("overlap selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
