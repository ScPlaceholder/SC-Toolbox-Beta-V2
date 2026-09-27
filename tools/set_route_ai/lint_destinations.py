"""lint_destinations.py - which hand-maintained destinations can the game NOT route to?

J asked for this on 2026-09-27 after we measured that the scunpacked datamine covers only ~1/3 of
`data/destinations.json` and therefore cannot replace it. The datamine is still worth something, and
it is NOT the names:

    starmap.json carries HideInStarmap, BlockTravel, NavIcon and ParentUUID.
    destinations.json has no field for any of that.

So this reads the datamine for FLAGS, never for names, and answers one question per entry: if you say
this out loud, can the in-game starmap actually plot it?

⛔⛔ THE RULE THIS FILE IS BUILT AROUND, AND IT IS THE WHOLE DESIGN:

    "NOT IN THE DATAMINE" IS A CANNOT-TELL, NOT A DEFECT.

Measured 2026-09-27, unfiltered and generously (every name from starmap.json + trade_locations.json
+ starmap_positions.json, 1,577 unique): **883 of the 1,317 hand-maintained destinations do not
appear at all.** And the absences are not junk — "aaron halo", "abyss", "adira falls", "aemilia",
"ailka belt alpha". Systems, belts and regions you genuinely route to, which the datamine simply does
not name as destinations.

A lint that reported those 883 as dead entries would be wrong 883 times and confident each time. So
only a POSITIVE flag is evidence here. UNKNOWN is the largest bucket by design and it is not a
finding. [[an-absence-needs-every-path-right]]

Verdicts, and the asymmetry is deliberate:
    BLOCKED    starmap says BlockTravel      -> a real finding. You cannot plot this.
    HIDDEN     starmap says HideInStarmap    -> a real finding. It is not in the UI to be clicked.
    OK         found, both flags clear       -> positively confirmed routable
    UNKNOWN    not in the datamine           -> NO CLAIM. Not a defect, not a pass.

Exit codes: 0 clean or unknown-only, 1 at least one BLOCKED/HIDDEN found, 2 could not run
(no cache and no network, unreadable input).

    python lint_destinations.py                 # summary + every finding
    python lint_destinations.py --all            # also list the OK and UNKNOWN entries
    python lint_destinations.py --refresh        # re-fetch starmap.json even if cached
    python lint_destinations.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

# Pinned deliberately: an unpinned ref means the lint's verdicts change under me between runs and I
# cannot tell a game patch from a data-source edit. Bump it on purpose, with a note.
SCUNPACKED_REPO = "StarCitizenWiki/scunpacked-data"
SCUNPACKED_REF = os.environ.get("SC_STARMAP_REF", "e96132078ae6")
STARMAP_PATH = "starmap.json"

HERE = Path(__file__).parent
DESTINATIONS = HERE / "data" / "destinations.json"
# Beside the other toolbox caches, NOT inside the skill folder: anything that replaces the folder
# would take the cache with it, and this is derived data that costs a 2 MB download to rebuild.
CACHE_DIR = Path(os.environ.get("SC_STARMAP_CACHE", Path.home() / ".sctoolbox" / "starmap"))

BLOCKED, HIDDEN, OK, UNKNOWN = "BLOCKED", "HIDDEN", "OK", "UNKNOWN"


def normalise(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    ⚠ The collapse matters and it is not cosmetic. Some hand-maintained keys carry artifacts from
      whatever generated them -- "ailka  kyukya" has a DOUBLE space -- so a naive comparison misses
      real matches and inflates UNKNOWN. Collapsing is why the coverage figure is an order of
      magnitude rather than a precise count, and the summary says so out loud.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (name or "").lower())).strip()


def starmap_url(ref: str = SCUNPACKED_REF) -> str:
    return f"https://raw.githubusercontent.com/{SCUNPACKED_REPO}/{ref}/{STARMAP_PATH}"


def load_starmap(refresh: bool = False, fetch=None, cache_dir: Path = None) -> list:
    """The datamine, cached by REF so a bumped pin cannot read the old file.

    `fetch` is injected so the selftest never touches the network -- the same shape sc_dev_history.py
    uses, and for the same reason: a test that needs GitHub is a test that fails on a train.

    ⛔⛔ `cache_dir` EXISTS BECAUSE THE FIRST VERSION'S SELFTEST POISONED THE PRODUCTION CACHE, and it
       did so within a minute of being written. The injected fetch returned `[{"Name": "Injected"}]`,
       this function cached it unconditionally to ~/.sctoolbox/starmap/, and the very next real run
       reported **"1317 destinations against 1 named starmap entry -- UNKNOWN 1317"**. A totally
       clean-looking board built on one fake row.
       ★ Two guards, and the redundancy is the point:
         1. `cache_dir` is a PARAMETER and the selftest passes a temp dir. Explicit.
         2. an injected `fetch` NEVER writes the shared cache, even if someone forgets (1).
       ⚠ Guard 2 alone would have been the tempting fix and it is the weaker one: it makes the safety
         implicit in a coincidence ("a fake fetch means a test"), which is true today and is exactly
         the kind of thing a later refactor breaks silently.
       [[a-probe-does-not-feel-like-production]]
    """
    cdir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    cache = cdir / f"starmap-{SCUNPACKED_REF}.json"
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass            # a corrupt cache is a reason to re-fetch, not to fail
    getter = fetch or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "SC-Toolbox-LintDestinations/1"}),
        timeout=60).read())
    raw = getter(starmap_url())
    data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if fetch is not None and cache_dir is None:
        return data         # guard 2: a test's data never reaches the shared cache
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass                # caching is a convenience; failing to cache is not failing to lint
    return data


def index_starmap(entries: list) -> dict:
    """normalised name -> {'blocked': bool, 'hidden': bool, 'name': original}

    ⚠ A name can appear more than once (two moons of the same name in different systems). When
      duplicates disagree, the OPTIMISTIC reading wins: if ANY entry with this name is routable, the
      player can route to something by that name, so calling it blocked would be a false finding.
      That is the same asymmetry as UNKNOWN -- this file only ever asserts a fault it can prove.
    """
    out: dict = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = normalise(e.get("Name"))
        if not key:
            continue
        blocked = bool(e.get("BlockTravel"))
        hidden = bool(e.get("HideInStarmap"))
        prev = out.get(key)
        if prev is None:
            out[key] = {"blocked": blocked, "hidden": hidden, "name": e.get("Name")}
        else:
            prev["blocked"] = prev["blocked"] and blocked
            prev["hidden"] = prev["hidden"] and hidden
    return out


def classify(dest_name: str, index: dict) -> tuple:
    """(verdict, detail). Only a POSITIVE flag is ever a finding."""
    hit = index.get(normalise(dest_name))
    if hit is None:
        return UNKNOWN, "not named in the datamine — NO CLAIM either way"
    if hit["blocked"]:
        return BLOCKED, "starmap says BlockTravel: the game will not plot a route here"
    if hit["hidden"]:
        return HIDDEN, "starmap says HideInStarmap: not present in the UI to be selected"
    return OK, "found, BlockTravel and HideInStarmap both clear"


def lint(destinations: dict, index: dict) -> dict:
    buckets = {BLOCKED: [], HIDDEN: [], OK: [], UNKNOWN: []}
    for name in destinations:
        verdict, detail = classify(name, index)
        buckets[verdict].append((name, detail))
    return buckets


def main(argv=None) -> int:
    # ⛔ FOURTH TOOL IN THIS HOUSE TO CRASH ON A BARE RUN OVER ONE '⚠'. The first version of this
    #   file printed the whole summary, then died with UnicodeEncodeError on the caveat line -- so a
    #   reader saw the COUNTS and not the sentence saying UNKNOWN is not a defect list. Worse, the
    #   traceback exits non-zero, which is indistinguishable from "found a real finding" to a caller.
    #   Wrapped, because a linter must not die configuring its own output.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not DESTINATIONS.exists():
        print("lint_destinations: CANNOT RUN — %s is missing" % DESTINATIONS)
        return 2
    try:
        destinations = json.loads(DESTINATIONS.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print("lint_destinations: CANNOT RUN — %s is unreadable: %s" % (DESTINATIONS, exc))
        return 2
    try:
        entries = load_starmap(refresh="--refresh" in argv)
    except Exception as exc:
        print("lint_destinations: CANNOT RUN — no cached starmap and the fetch failed: %s: %s"
              % (type(exc).__name__, exc))
        print("  This is a CANNOT-TELL, not a clean bill of health. Nothing was checked.")
        return 2

    index = index_starmap(entries)
    buckets = lint(destinations, index)
    n = len(destinations)

    print("lint_destinations: %d hand-maintained destination(s) against %d named starmap entr(ies)"
          % (n, len(index)))
    print("  scunpacked %s @ %s" % (SCUNPACKED_REPO, SCUNPACKED_REF))
    print()
    print("  BLOCKED %4d   cannot be routed to — a real finding" % len(buckets[BLOCKED]))
    print("  HIDDEN  %4d   not selectable in the starmap UI — a real finding" % len(buckets[HIDDEN]))
    print("  OK      %4d   positively confirmed routable" % len(buckets[OK]))
    print("  UNKNOWN %4d   not in the datamine — NO CLAIM, and this is the EXPECTED majority"
          % len(buckets[UNKNOWN]))
    print()

    for verdict in (BLOCKED, HIDDEN):
        for name, detail in sorted(buckets[verdict]):
            print("  [%s] %s — %s" % (verdict, name, detail))

    if "--all" in argv:
        for verdict in (OK, UNKNOWN):
            print()
            print("  == %s (%d)" % (verdict, len(buckets[verdict])))
            for name, _ in sorted(buckets[verdict]):
                print("     %s" % name)

    print()
    print("  ⚠ UNKNOWN IS NOT A DEFECT LIST. Measured 2026-09-27: 883 of 1,317 entries are absent")
    print("    from the datamine, including 'aaron halo', 'adira falls' and 'ailka belt alpha' —")
    print("    systems, belts and regions the datamine does not name as destinations. Only a")
    print("    POSITIVE BlockTravel/HideInStarmap flag is evidence here.")
    print("  ⚠ Name matching collapses whitespace and strips punctuation, because some keys carry")
    print("    generator artifacts ('ailka  kyukya' has a double space). Counts are the right order")
    print("    of magnitude, not exact.")

    findings = len(buckets[BLOCKED]) + len(buckets[HIDDEN])
    print()
    print("== lint_destinations COMPLETE (rc=%d) ==" % (1 if findings else 0))
    return 1 if findings else 0


def _selftest() -> int:
    fails = []

    # ★ THE CONTROL PAIR. Every arm below shares the same shape and differs in ONE flag, so an
    #   implementation that ignores the flags cannot pass: it must either report everything as OK
    #   (the BLOCKED/HIDDEN arms fail) or everything as a finding (the OK arm fails).
    entries = [
        {"Name": "Orison", "BlockTravel": False, "HideInStarmap": False},
        {"Name": "Dead Place", "BlockTravel": True, "HideInStarmap": False},
        {"Name": "Secret Place", "BlockTravel": False, "HideInStarmap": True},
        {"Name": "Ailka  Kyukya", "BlockTravel": False, "HideInStarmap": False},   # double space
    ]
    idx = index_starmap(entries)
    cases = [
        ("Orison", OK), ("orison", OK),
        ("Dead Place", BLOCKED),
        ("Secret Place", HIDDEN),
        ("Aaron Halo", UNKNOWN),                    # absent -> must NOT be a finding
        ("Ailka Kyukya", OK),                       # single space must match the double-space key
    ]
    for name, want in cases:
        got, _ = classify(name, idx)
        if got != want:
            fails.append("classify(%r) = %s, wanted %s" % (name, got, want))

    # ⚠ UNKNOWN must not raise the exit code. This is the assertion that stops a future "tidy-up"
    #   from folding UNKNOWN into the findings, which would make the tool wrong 883 times.
    b = lint({"Aaron Halo": {}, "Abyss": {}}, idx)
    if b[UNKNOWN] and (b[BLOCKED] or b[HIDDEN]):
        fails.append("an all-UNKNOWN input produced findings — absence became a defect")

    # duplicate names: optimistic wins, because one routable match means the player can route
    dupes = index_starmap([{"Name": "Twin", "BlockTravel": True},
                           {"Name": "Twin", "BlockTravel": False}])
    if classify("Twin", dupes)[0] != OK:
        fails.append("duplicate names: a routable twin must beat a blocked one, got %s"
                     % classify("Twin", dupes)[0])

    # fetch injection: the loader must never need the network in a test, and must never write the
    # shared cache. Both are asserted, because the first version of this selftest DID write it.
    import tempfile
    probe = {"seen": False}

    def fake(url):
        probe["seen"] = True
        return json.dumps([{"Name": "Injected"}]).encode("utf-8")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            got = load_starmap(refresh=True, fetch=fake, cache_dir=tmp)
            if not probe["seen"] or not got or got[0].get("Name") != "Injected":
                fails.append("fetch injection did not take effect")
            if not (Path(tmp) / ("starmap-%s.json" % SCUNPACKED_REF)).exists():
                fails.append("cache_dir was passed but nothing was written there — the override is "
                             "not actually in use, so the production cache may still be the target")
    except Exception as exc:
        fails.append("load_starmap with an injected fetch raised %s: %s" % (type(exc).__name__, exc))

    # ⛔⛔ THE REGRESSION TEST FOR THE BUG THIS SELFTEST CAUSED. With a fake fetch and NO cache_dir,
    #   nothing may reach the real cache. Asserted by mtime/absence on the actual production path,
    #   because checking a mock would test the mock. If this ever fails, a real run afterwards will
    #   read one fake row and report a beautifully clean 1317/UNKNOWN board.
    real = CACHE_DIR / ("starmap-%s.json" % SCUNPACKED_REF)
    before = real.stat().st_mtime if real.exists() else None
    try:
        load_starmap(refresh=True, fetch=fake)
        after = real.stat().st_mtime if real.exists() else None
        if before != after:
            fails.append("★★ A TEST FETCH WROTE THE PRODUCTION CACHE (%s). This is the exact defect "
                         "that made the first real run report 1 starmap entry." % real)
    except Exception as exc:
        fails.append("the no-cache_dir guard raised %s: %s" % (type(exc).__name__, exc))

    print("lint_destinations selftest:", "PASS" if not fails else "FAIL")
    for f in fails:
        print("   -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else main())
