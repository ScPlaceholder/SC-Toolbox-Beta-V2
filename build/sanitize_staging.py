"""SANITIZE STAGING (Elah 2026-07-22) — complete the build's privacy scrub before a public release.

The build's own sanitizer strips torch metadata from SOME onnx models but misses dev/train scripts, model
.json sidecars, runtime .py path constants, and backup model dirs — all embedding the build machine's
absolute path C:\\Users\\<username>\\... . A scan of the 2.3.0 staging found ~78 files leaking the username.

Strategy (chosen to be SAFE first, complete second):
  * SCRUB is the primary fix. Same-LENGTH byte replacement of the username token (e.g. youruser -> _user,
    5->5 chars) in every shippable text/model file. Same length keeps ONNX protobuf offsets valid (binary
    safe) and catches EVERY escaping variant (single-slash in .py raw strings, double-slash in JSON). It
    can't break a runtime import because it only neutralizes a dead/leaked path string, never structure.
  * PRUNE only the model BACKUP dirs (_bak_*, models_bak_*) under tools/Mining_Signals/ocr — unambiguously
    non-runtime, and this also removes the model that failed onnx metadata-strip. NO dev-script pruning
    (too easy to catch a vendored or runtime-imported file; the scrub already kills those leaks).

Only touches tools/Mining_Signals (the project tree). Leaves the bundled python env alone.

Usage:
  python sanitize_staging.py <staging_dir> --user youruser --repl _user           # DRY RUN (report only)
  python sanitize_staging.py <staging_dir> --user youruser --repl _user --apply    # prune + scrub
Exit 0 = clean/dry-ok, 2 = leaks remain after apply, 3 = usage/length error.
"""
import os, re, sys, shutil, argparse

BACKUP_DIR_RE = re.compile(r"(^_bak_|^models_bak_|^_bak$)", re.I)
SHIP_EXTS = (".json", ".py", ".onnx", ".txt", ".yaml", ".yml", ".cfg", ".ini", ".md", ".pdmodel", ".pdiparams")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("staging")
    ap.add_argument("--user", required=True, help="username token to scrub (e.g. youruser)")
    ap.add_argument("--repl", default=None,
                    help="same-length replacement token; if omitted, auto = same-length redaction so the "
                         "build script can pass just --user %USERNAME% without hardcoding a name")
    ap.add_argument("--apply", action="store_true", help="actually modify (default: dry run)")
    a = ap.parse_args()
    staging = os.path.abspath(a.staging)
    tok = a.user
    if not tok:
        print("[!] --user is empty"); sys.exit(3)
    # auto same-length redaction when --repl not given: 'scusr' style, padded/truncated with '_' to match.
    repl = a.repl if a.repl is not None else ("scusr" + "_" * len(tok))[:len(tok)] if len(tok) >= 5 else "_" * len(tok)
    if len(tok) != len(repl):
        print(f"[!] --user ({len(tok)}) and --repl ({len(repl)}) must be SAME length (binary safety)."); sys.exit(3)
    if tok == repl:
        print("[!] replacement equals token — refusing (would be a no-op)"); sys.exit(3)
    mining = os.path.join(staging, "tools", "Mining_Signals")
    if not os.path.isdir(mining):
        print("tools/Mining_Signals not found under staging:", staging); sys.exit(3)
    dry = not a.apply
    tag = "[DRY]" if dry else "[APPLY]"
    tokb, replb = tok.encode(), repl.encode()

    pruned_dirs = scrubbed = 0

    # PASS 1: prune backup model dirs only (safe; also drops strip-failed models)
    for root, dirs, files in os.walk(mining, topdown=True):
        for d in list(dirs):
            if BACKUP_DIR_RE.search(d):
                p = os.path.join(root, d)
                print(f"{tag} prune dir : {os.path.relpath(p, staging)}")
                if not dry: shutil.rmtree(p, ignore_errors=True)
                pruned_dirs += 1
                dirs.remove(d)

    # PASS 2: same-length token scrub across EVERY file under the project tree (no extension filter —
    # the leak also hides in .csv/.out training logs; same-length byte replace is safe for any file type).
    for root, dirs, files in os.walk(mining):
        if BACKUP_DIR_RE.search(os.path.basename(root)):
            continue
        for f in files:
            fp = os.path.join(root, f)
            try:
                raw = open(fp, "rb").read()
            except Exception:
                continue
            if tokb not in raw:
                continue
            new = raw.replace(tokb, replb)
            print(f"{tag} scrub {raw.count(tokb):>3}x: {os.path.relpath(fp, staging).replace(chr(92),'/')}")
            if not dry: open(fp, "wb").write(new)
            scrubbed += 1

    # PASS 3: verify no token remains anywhere shippable under the project tree
    remaining = []
    for root, dirs, files in os.walk(mining):
        if not dry and BACKUP_DIR_RE.search(os.path.basename(root)):
            continue
        for f in files:
            fp = os.path.join(root, f)
            try:
                if tokb in open(fp, "rb").read():
                    remaining.append(os.path.relpath(fp, staging).replace("\\", "/"))
            except Exception:
                pass

    print(f"\n{tag} summary: prune_dirs={pruned_dirs} scrubbed_files={scrubbed}")
    if dry:
        print(f"[DRY] after prune+scrub, files that would still contain '{tok}': "
              f"{len([r for r in remaining if not any(b in r for b in ('_bak_','models_bak_'))])} "
              f"(backup dirs excluded since they'd be pruned)")
        sys.exit(0)
    if remaining:
        print(f"[APPLY] LEAKS REMAIN ({len(remaining)}):")
        for r in remaining[:40]: print("   ", r)
        sys.exit(2)
    print(f"[APPLY] CLEAN — zero occurrences of '{tok}' under tools/Mining_Signals.")
    sys.exit(0)

if __name__ == "__main__":
    main()
