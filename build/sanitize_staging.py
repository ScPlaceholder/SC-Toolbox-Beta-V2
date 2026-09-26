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
    # ★★★ 2026-08-17 — the DOUBLED %% IS REQUIRED. argparse runs help strings through
    #   %-formatting, so a literal "%USERNAME%" raises ValueError: badly formed help string at
    #   add_argument() time. This script therefore died before parsing a single argument, EVERY
    #   time it was invoked. The build called it, got the crash, and correctly refused to continue:
    #   "Privacy scrub FAILED — username may still be present in staging. Aborting to avoid a
    #   public leak."
    #   So the guard worked perfectly and the thing it guards has never once run. The scrub was
    #   written 2026-07-22 to fix ~78 files leaking the username, and it has been dead since.
    #   Nothing noticed because the abort looks exactly like the guard doing its job.
    #   [[test-what-the-instrument-CANNOT-distinguish]] — "tool refused" and "tool is broken"
    #   produce the same abort message.
    ap.add_argument("--repl", default=None,
                    help="same-length replacement token; if omitted, auto = same-length redaction so the "
                         "build script can pass just --user %%USERNAME%% without hardcoding a name")
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

    pruned_dirs = scrubbed = pruned_ckpt = ckpt_bytes = pruned_lang = lang_bytes = 0

    # PASS 1: prune backup model dirs only (safe; also drops strip-failed models)
    for root, dirs, files in os.walk(mining, topdown=True):
        for d in list(dirs):
            if BACKUP_DIR_RE.search(d):
                p = os.path.join(root, d)
                print(f"{tag} prune dir : {os.path.relpath(p, staging)}")
                if not dry: shutil.rmtree(p, ignore_errors=True)
                pruned_dirs += 1
                dirs.remove(d)

    # PASS 1z (2026-09-26): drop archive files sitting at the tool's top level. The 2.4.0 test build
    # shipped tools/Mining_Signals/ocr.zip, a 625 MB dev archive that nothing in the tree references
    # (the tool's own .gitignore says so) -- untracked, but the build copies the whole folder. It
    # alone made the full package bigger than 2.3.1 despite ~540 MB of other pruning, and turned a
    # would-be small update into a 1.2 GB delta. Top level only, so no runtime data inside ocr/ etc.
    # can be caught by accident; fails the build if the delete does not stick.
    for f in sorted(os.listdir(mining)):
        fp = os.path.join(mining, f)
        if os.path.isfile(fp) and f.lower().endswith((".zip", ".7z", ".rar", ".tar", ".gz")):
            print(f"{tag} prune file: {os.path.relpath(fp, staging)} ({os.path.getsize(fp) / 1e6:.0f} MB archive)")
            if not dry:
                os.remove(fp)
                if os.path.exists(fp):
                    print(f"{tag} FAILED to remove {fp}")
                    sys.exit(2)

    # PASS 1a (2026-09-25): drop live_samples/ entirely. It is a debug capture folder, and the
    # switch that turns capture ON is a file inside it (live_samples/.enabled, read by
    # ocr/screen_reader.py:_dump_live_sample). 2.3.1 shipped that switch, so every user's copy saved a
    # PNG of the signal panel on every scan, without limit, plus the dev machine's 2,109 captures.
    # Nothing else reads the folder; without it the dump is inert (one env lookup, one path check).
    live = os.path.join(mining, "live_samples")
    if os.path.isdir(live):
        n_live = sum(len(fs) for _, _, fs in os.walk(live))
        print(f"{tag} prune dir : {os.path.relpath(live, staging)} ({n_live} files, incl. the .enabled switch)")
        if not dry:
            shutil.rmtree(live, ignore_errors=True)
            if os.path.exists(os.path.join(live, ".enabled")):
                print(f"{tag} FAILED to remove {live}/.enabled - live capture would ship ON")
                sys.exit(2)
        pruned_dirs += 1

    # PASS 1c (2026-09-25): keep only the Tesseract language data the app uses (~300 MB saved).
    # The build copies the WHOLE system Tesseract install into tools/Mining_Signals/tesseract, with
    # every language the build machine has. Every runtime call (ocr/sc_ocr/api.py, refinery_reader,
    # screen_reader, onnx_hud_reader) either passes "-l eng_sc" (served from ocr/tessdata, not this
    # folder), "-l eng", or no -l at all (Tesseract's default, eng). Page-segmentation modes in use are
    # 6, 7, 8 and 11; none needs OSD, but osd.traineddata is kept anyway as a cheap margin. Non-model
    # files (configs/, tessconfigs/, *.user-patterns etc.) are kept. build_installer.bat re-checks
    # tessdata/eng.traineddata AFTER this runs and fails the build if it is missing.
    KEEP_LANGS = {"eng.traineddata", "osd.traineddata"}
    tessdata = os.path.join(mining, "tesseract", "tessdata")
    if os.path.isdir(tessdata):
        for root, dirs, files in os.walk(tessdata):
            for f in files:
                if f.lower().endswith(".traineddata") and f.lower() not in KEEP_LANGS:
                    fp = os.path.join(root, f)
                    lang_bytes += os.path.getsize(fp)
                    pruned_lang += 1
                    if not dry: os.remove(fp)
        print(f"{tag} prune lang: {pruned_lang} Tesseract language files ({lang_bytes / 1e6:.0f} MB), "
              f"kept {sorted(KEEP_LANGS)}")
        if not dry and not os.path.isfile(os.path.join(tessdata, "eng.traineddata")):
            print(f"{tag} FAILED: tessdata/eng.traineddata is missing after the language prune")
            sys.exit(2)

    # PASS 1b (2026-09-25): prune PyTorch training checkpoints (*.pt / *.pth), ~153 MB in 2.3.1.
    # They cannot be used by a shipped copy: PyTorch is not in the bundled Python (checked: no
    # site-packages/torch anywhere in the 2.3.1 package). No runtime module references a .pt/.pth
    # path; only train_*/export_*/pretrain_* scripts and one sanity test do. Even the in-app online
    # learner (ocr/online_learner.py) seeds its PyTorch copy from the shipped ONNX weights, not from
    # these, and is a no-op without torch. They stay in the repo for retraining on the dev machine.
    for root, dirs, files in os.walk(mining):
        for f in files:
            if f.lower().endswith((".pt", ".pth")):
                fp = os.path.join(root, f)
                size = os.path.getsize(fp)
                print(f"{tag} prune ckpt: {os.path.relpath(fp, staging)} ({size / 1e6:.1f} MB)")
                if not dry: os.remove(fp)
                pruned_ckpt += 1
                ckpt_bytes += size

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

    print(f"\n{tag} summary: prune_dirs={pruned_dirs} pruned_checkpoints={pruned_ckpt} "
          f"({ckpt_bytes / 1e6:.0f} MB) scrubbed_files={scrubbed}")
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
