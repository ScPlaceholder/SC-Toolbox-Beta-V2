"""Plain-text report over per-scan telemetry records.

Reads the JSONL written by ``ocr/sc_ocr/scan_record.py`` (one record per
completed scan of the panel-OCR capture pipeline) and prints a summary:

  OVERVIEW   throughput, wall-time span, scan-duration percentiles
  PER-FIELD  non-null rate, distinct values, flap count (consecutive
             non-null value changes), most common value, last value
  POSE       pose-lock rate and hold-streak statistics
  REFLEX     value-consistency gate-bypass revocations per field
  TIMELINE   optional (n, value) dump for one field via --field

Record schema (see scan_record.write): n, ts, ms, mass, resistance,
instability, mineral, plus optional pose_holds, pose_set and
bypass_revoked (list of field names with their gate bypass currently
revoked by the consistency reflex).

Read-only diagnostics; never modifies the record file. ``--selftest``
runs the full report against a synthetic temp file (no display
interaction) and verifies the arithmetic.
"""

import platform
platform._wmi = None  # noqa: E402  (Py3.14 WMI import hang guard)

import argparse
import json
import os
import statistics
import time

FIELDS = ("mass", "resistance", "instability", "mineral")
REFLEX_FIELDS = ("mass", "resistance", "instability")
TIMELINE_LEN = 30
ONSETS_SHOWN = 5

# scripts/ -> Mining_Signals/ -> debug_glyphs/scan_records.jsonl
# (mirrors the producer's own path derivation, so the report always
# reads the telemetry of the tree it lives in)
DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "debug_glyphs", "scan_records.jsonl",
)


# --------------------------------------------------------------- helpers

def _fmt(v):
    """Compact display form; '-' for null."""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "%.10g" % v
    return str(v)


def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _hms(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return "?"


def _table(rows, aligns, indent="  "):
    """Pad columns to a shared width. ``aligns`` is one 'l'/'r' per column."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for r in rows:
        cells = [
            (c.rjust(w) if a == "r" else c.ljust(w))
            for c, w, a in zip(r, widths, aligns)
        ]
        lines.append((indent + "  ".join(cells)).rstrip())
    return lines


def flap_count(values):
    """Consecutive-record changes where both values are non-null and differ."""
    flaps = 0
    for prev, cur in zip(values, values[1:]):
        if prev is not None and cur is not None and cur != prev:
            flaps += 1
    return flaps


# --------------------------------------------------------------- loading

def load_records(path, last_n):
    """Parse the JSONL file -> (records, total_parsed, skipped_lines).

    Keeps only the most recent ``last_n`` records when last_n > 0.
    Malformed lines (e.g. a line cut by file rotation) are skipped.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    records, skipped = [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            skipped += 1
    total = len(records)
    if last_n and last_n > 0 and total > last_n:
        records = records[-last_n:]
    return records, total, skipped


# -------------------------------------------------------------- analysis

def analyze(records):
    """Compute every statistic the report renders, as one dict."""
    a = {"count": len(records)}

    # overview -----------------------------------------------------------
    ts = [r["ts"] for r in records if isinstance(r.get("ts"), (int, float))]
    a["ts_first"] = ts[0] if ts else None
    a["ts_last"] = ts[-1] if ts else None
    span = (ts[-1] - ts[0]) if len(ts) >= 2 else 0.0
    a["span_s"] = span
    a["scans_per_min"] = (
        60.0 * (len(ts) - 1) / span if span > 0 and len(ts) >= 2 else None
    )
    ms = sorted(
        float(r["ms"]) for r in records if isinstance(r.get("ms"), (int, float))
    )
    if ms:
        a["ms_med"] = statistics.median(ms)
        a["ms_p90"] = ms[(9 * len(ms) + 9) // 10 - 1]  # nearest-rank p90
        a["ms_max"] = ms[-1]
    else:
        a["ms_med"] = a["ms_p90"] = a["ms_max"] = None

    # per-field ------------------------------------------------------------
    a["fields"] = {}
    for f in FIELDS:
        vals = [r.get(f) for r in records]
        nonnull = [v for v in vals if v is not None]
        counts = {}
        for v in nonnull:
            counts[v] = counts.get(v, 0) + 1
        top = None
        for v, c in counts.items():  # first-seen value wins ties
            if top is None or c > top[1]:
                top = (v, c)
        a["fields"][f] = {
            "nonnull": len(nonnull),
            "rate": _pct(len(nonnull), len(vals)),
            "distinct": len(counts),
            "flaps": flap_count(vals),
            "top": top,                       # (value, count) or None
            "last": vals[-1] if vals else None,
        }

    # pose -----------------------------------------------------------------
    pose_records = 0
    increments = resets = 0
    longest = None
    prev_h = None
    for r in records:
        h = r.get("pose_holds")
        if isinstance(h, bool) or not isinstance(h, int):
            prev_h = None  # record without pose data breaks the chain
            continue
        pose_records += 1
        longest = h if longest is None else max(longest, h)
        if prev_h is not None:
            if h > prev_h:
                increments += 1          # one more consecutive hold detected
            elif h == 0 and prev_h > 0:
                resets += 1              # streak broken
        prev_h = h
    a["pose_records"] = pose_records
    a["pose_set_true"] = sum(1 for r in records if r.get("pose_set") is True)
    a["pose_inc"] = increments
    a["pose_resets"] = resets
    a["pose_longest"] = longest

    # reflex ---------------------------------------------------------------
    reflex_total = 0
    per_field = {}
    prev_revoked = frozenset()
    for r in records:
        raw = r.get("bypass_revoked")
        revoked = (
            frozenset(x for x in raw if isinstance(x, str))
            if isinstance(raw, list) else frozenset()
        )
        if revoked:
            reflex_total += 1
        for f in revoked:
            d = per_field.setdefault(f, {"active": 0, "onsets": []})
            d["active"] += 1
            if f not in prev_revoked:    # newly revoked -> onset record
                d["onsets"].append(r.get("n"))
        prev_revoked = revoked
    a["reflex_total"] = reflex_total
    a["reflex_per_field"] = per_field
    return a


# -------------------------------------------------------------- rendering

def render(records, a, path, total=None, skipped=0, field=None):
    """Return the report as a list of lines."""
    out = []
    n = a["count"]
    out.append("SCAN RECORD REPORT")
    out.append("  source : %s" % path)
    if total is not None and total != n:
        out.append("  window : last %d of %d parsed records" % (n, total))
    if skipped:
        out.append("  note   : %d unparseable line(s) skipped" % skipped)
    out.append("")

    # 1. OVERVIEW ----------------------------------------------------------
    out.append("OVERVIEW")
    rows = [("records", "%d" % n)]
    if a["ts_first"] is not None:
        span_txt = "%s .. %s local" % (_hms(a["ts_first"]), _hms(a["ts_last"]))
        if a["span_s"] > 0:
            span_txt += "  (%.1f s)" % a["span_s"]
        rows.append(("span", span_txt))
    rows.append((
        "scans/min",
        "%.1f" % a["scans_per_min"] if a["scans_per_min"] is not None else "n/a",
    ))
    if a["ms_med"] is not None:
        rows.append(("duration ms", "med %s   p90 %s   max %s" % (
            _fmt(a["ms_med"]), _fmt(a["ms_p90"]), _fmt(a["ms_max"]))))
    out.extend(_table(rows, "ll"))
    out.append("")

    # 2. PER-FIELD ---------------------------------------------------------
    out.append("PER-FIELD")
    rows = [("field", "nonnull%", "distinct", "flaps", "top (share%)", "last")]
    for f in FIELDS:
        s = a["fields"][f]
        top = "-"
        if s["top"] is not None:
            top = "%s (%.1f%%)" % (
                _fmt(s["top"][0]), _pct(s["top"][1], s["nonnull"]))
        rows.append((f, "%.1f" % s["rate"], "%d" % s["distinct"],
                     "%d" % s["flaps"], top, _fmt(s["last"])))
    out.extend(_table(rows, "lrrrll"))
    out.append("")

    # 3. POSE --------------------------------------------------------------
    out.append("POSE")
    if a["pose_records"] == 0:
        out.append("  no pose telemetry in window")
    else:
        rows = [
            ("pose_set true", "%.1f%%  (%d/%d)" % (
                _pct(a["pose_set_true"], n), a["pose_set_true"], n)),
            ("hold increments (detections)", "%d" % a["pose_inc"]),
            ("hold resets to 0", "%d" % a["pose_resets"]),
            ("longest hold streak", _fmt(a["pose_longest"])),
        ]
        out.extend(_table(rows, "ll"))
    out.append("")

    # 4. REFLEX ------------------------------------------------------------
    out.append("REFLEX (value-consistency gate-bypass revocations)")
    out.append("  records with any field revoked   %d  (%.1f%%)" % (
        a["reflex_total"], _pct(a["reflex_total"], n)))
    names = list(REFLEX_FIELDS) + sorted(
        set(a["reflex_per_field"]) - set(REFLEX_FIELDS))
    rows = [("field", "active-records", "onset n (first %d)" % ONSETS_SHOWN)]
    for f in names:
        d = a["reflex_per_field"].get(f)
        if d is None:
            rows.append((f, "0", "-"))
            continue
        onsets = ", ".join(_fmt(x) for x in d["onsets"][:ONSETS_SHOWN])
        if len(d["onsets"]) > ONSETS_SHOWN:
            onsets += "  (+%d more)" % (len(d["onsets"]) - ONSETS_SHOWN)
        rows.append((f, "%d" % d["active"], onsets))
    out.extend(_table(rows, "lrl"))

    # 5. TIMELINE ----------------------------------------------------------
    if field:
        out.append("")
        tail = records[-TIMELINE_LEN:]
        start = len(records) - len(tail)
        out.append("TIMELINE: %s  (last %d of %d records; * marks a value change)"
                   % (field, len(tail), len(records)))
        rows = [("", "n", "value")]
        for i, rec in enumerate(tail):
            idx = start + i
            cur = rec.get(field)
            mark = ""
            if idx > 0:
                prev = records[idx - 1].get(field)
                if prev is not None and cur is not None and cur != prev:
                    mark = "*"
            rows.append((mark, _fmt(rec.get("n")), _fmt(cur)))
        out.extend(_table(rows, "lrl"))
    return out


# -------------------------------------------------------------- selftest

def _synthetic_records():
    """20-record fixture with known flap / pose / reflex arithmetic.

    mass        stable 3384.0 throughout            -> 0 flaps
    resistance  52 (n1-10), 100 (n11), 11 (n12),
                52 (n13-20)                          -> 3 flaps
    instability null (n1-4), 1.43 (n5-14), null
                (n15), 2.1 (n16-20)                  -> 0 flaps (null-adjacent
                                                       changes never count)
    mineral     Aluminum, null at n3                 -> 0 flaps
    pose_holds  0..5 over n1-6, 0 from n7, keys
                absent at n20                        -> 5 increments, 1 reset
    bypass_revoked  resistance at n11-13,
                instability at n12 only              -> onsets 11 and 12
    """
    base = 1781310000.0
    recs = []
    for i in range(1, 21):
        rec = {
            "n": i,
            "ts": base + 2.0 * (i - 1),
            "ms": 900.0 + 10.0 * i,
            "mass": 3384.0,
            "resistance": 100.0 if i == 11 else (11.0 if i == 12 else 52.0),
            "instability": (None if i <= 4 or i == 15
                            else (1.43 if i <= 14 else 2.1)),
            "mineral": None if i == 3 else "Aluminum",
        }
        if i <= 19:  # n20 omits pose keys (they are optional in the schema)
            rec["pose_holds"] = i - 1 if i <= 6 else 0
            rec["pose_set"] = 2 <= i <= 6
        if 11 <= i <= 13:
            rec["bypass_revoked"] = (
                ["resistance", "instability"] if i == 12 else ["resistance"])
        recs.append(rec)
    return recs


def selftest():
    tmpdir = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    tmp = os.path.join(tmpdir, "scan_records_selftest_%d.jsonl" % os.getpid())
    checks = []

    def check(cond, what):
        if not cond:
            print("SELFTEST FAIL: %s" % what)
            raise SystemExit(1)
        checks.append(what)

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in _synthetic_records():
                f.write(json.dumps(rec) + "\n")

        records, total, skipped = load_records(tmp, 500)
        a = analyze(records)
        text = "\n".join(render(records, a, tmp, total=total,
                                skipped=skipped, field="resistance"))
        print(text)
        print()

        check(a["count"] == 20 and skipped == 0, "20 records parsed")
        fs = a["fields"]
        check(fs["resistance"]["flaps"] == 3, "resistance flap count == 3")
        check(fs["mass"]["flaps"] == 0, "mass flap count == 0")
        check(fs["instability"]["flaps"] == 0,
              "null-adjacent changes are not flaps")
        check(fs["mineral"]["flaps"] == 0, "mineral flap count == 0")
        check(fs["mass"]["distinct"] == 1 and fs["mass"]["rate"] == 100.0,
              "mass distinct == 1, nonnull 100%")
        check(abs(fs["instability"]["rate"] - 75.0) < 1e-9,
              "instability nonnull rate == 75%")
        check(fs["instability"]["distinct"] == 2, "instability distinct == 2")
        check(fs["resistance"]["top"] == (52.0, 18),
              "resistance top value 52 (x18)")
        check(fs["resistance"]["last"] == 52.0, "resistance last value == 52")
        check(a["reflex_total"] == 3, "3 records carry revocations")
        rp = a["reflex_per_field"]
        check(rp["resistance"]["active"] == 3,
              "resistance revocation-active in 3 records")
        check(rp["resistance"]["onsets"] == [11],
              "resistance first revoked at n=11")
        check(rp["instability"]["active"] == 1
              and rp["instability"]["onsets"] == [12],
              "instability revoked only at n=12")
        check("mass" not in rp, "mass never revoked")
        check(a["pose_inc"] == 5, "pose hold increments (detections) == 5")
        check(a["pose_resets"] == 1, "pose hold resets == 1")
        check(a["pose_longest"] == 5, "longest hold streak == 5")
        check(a["pose_set_true"] == 5, "pose_set true in 5 records")
        check(abs(a["scans_per_min"] - 30.0) < 1e-9, "scans/min == 30.0")
        check((a["ms_med"], a["ms_p90"], a["ms_max"]) == (1005.0, 1080.0, 1100.0),
              "duration med/p90/max == 1005/1080/1100")
        for section in ("OVERVIEW", "PER-FIELD", "POSE", "REFLEX", "TIMELINE"):
            check(section in text, "report has %s section" % section)
        marked = sum(1 for line in text.splitlines()
                     if line.lstrip().startswith("*"))
        check(marked == 3, "timeline shows exactly 3 flap markers")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    print("SELFTEST OK (%d checks)" % len(checks))
    return 0


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="scan_records_report",
        description="Plain-text report over per-scan telemetry records "
                    "(scan_records.jsonl) written by the capture pipeline.",
    )
    ap.add_argument("--file", default=DEFAULT_PATH,
                    help="JSONL record file (default: %(default)s)")
    ap.add_argument("--last", type=int, default=500, metavar="N",
                    help="analyze only the most recent N records "
                         "(default 500; 0 or negative = all)")
    ap.add_argument("--field", choices=FIELDS,
                    help="append a (n, value) timeline of the last %d "
                         "records for this field" % TIMELINE_LEN)
    ap.add_argument("--selftest", action="store_true",
                    help="run the full report against a synthetic temp file "
                         "and verify the arithmetic")
    args = ap.parse_args(argv)

    if args.selftest:
        raise SystemExit(selftest())

    path = args.file
    if not os.path.isfile(path):
        print("Nothing to report: record file not found: %s" % path)
        print("It appears after the capture pipeline completes its first scan.")
        raise SystemExit(0)
    try:
        records, total, skipped = load_records(path, args.last)
    except OSError as exc:
        print("Nothing to report: could not read %s (%s)" % (path, exc))
        raise SystemExit(0)
    if not records:
        print("Nothing to report: %s contains no parseable records yet." % path)
        raise SystemExit(0)

    a = analyze(records)
    print("\n".join(render(records, a, path, total=total,
                           skipped=skipped, field=args.field)))


if __name__ == "__main__":
    main()
