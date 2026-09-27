"""generate_assets.py — run the asset pipeline. STUBBED BY DEFAULT; real generation is opt-in.

⛔⛔ THIS FILE CAN SPEND J'S MONEY AND IT DEFAULTS TO NOT DOING SO. `--mode stub` is the default and
   draws every image locally. `--mode real` is the only path that calls a paid API, it requires
   `--i-mean-it`, and it refuses without a resolved credential. Two flags rather than one, because a
   single flag is one typo away from 26 billed requests and a default is what you get when tired.

WHAT THE DRY RUN ACTUALLY PROVES, which is the whole reason it exists before the paid half:
   - the manifest is complete and every asset has a destination
   - names land where the rig expects (skins/<SKIN>/<slot>.png)
   - the validator is wired in and its verdict routes the file
   - the REJECT path works — and it is exercised on purpose, see below
   - nothing overwrites anything

★ THE STUB DELIBERATELY FAILS SOME ASSETS. A dry run where everything passes proves the accept path
  and nothing else; the reject bin would be untested code that first runs on real, paid images. So
  the stub draws a known-bad image for a fixed, named subset (`FORCED_REJECTS`) and this module
  asserts at the end that those and ONLY those were rejected. A reject path that never fires during
  a rehearsal is not covered. [[a-correct-rule-can-guard-a-branch-nothing-takes]]

⚠ WHAT A STUB RUN CANNOT TELL ME: whether the real generator obeys the prompts, whether the art is
  any good, or whether the negatives work. It tests the PLUMBING. Saying so because a green dry run
  is exactly the kind of result I would otherwise quote as if it meant the pipeline was proven.

    python generate_assets.py                      # stub, writes to out/
    python generate_assets.py --selftest
    python generate_assets.py --mode real --i-mean-it --limit 1    # ONE paid image
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "assets", "asset_manifest.json")
OUT = os.path.join(HERE, "out")
REJECT_DIR = "REJECTED"

# Slots the stub renders deliberately broken, so the reject path runs in every rehearsal.
# ⚠ Chosen to fail DIFFERENT rules: one by coverage (whole-frame fill), one by an opaque
#   background. One forced reject would only ever exercise whichever rule it happened to trip.
FORCED_REJECTS = {
    "belly": "coverage",     # fills the frame -> "the generator drew the whole penguin"
    "beak": "opaque",        # background baked in -> not a real cutout
}


def _load_validator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("asset_validate",
                                                  os.path.join(HERE, "asset_validate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stub_image(path, kind, mode="good", size=1024):
    """Draw a placeholder locally. No network, no cost."""
    from PySide6.QtGui import QImage, QColor, QPainter

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 255) if mode == "opaque" else QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QColor(70, 150, 230, 255))
    p.setPen(QColor(20, 40, 70, 255))
    # ⚠ AN ELLIPSE IS NOT ITS BOUNDING BOX, and that cost me a forced reject. Drawing the
    #   coverage-failure case as an ellipse inscribed in a 0.92 square yields 0.92 * pi/4 = 0.72
    #   opaque — INSIDE the body band ceiling of 0.75 — so the asset the stub was supposed to fail
    #   sailed through, and the selftest correctly reported the reject path as not firing.
    #   The forced case is now a RECTANGLE, whose area is the number I asked for.
    if mode == "coverage":
        # inset 2%% so the border stays clear and this rejects on COVERAGE, not on the edge rule
        off = int(size * 0.02)
        p.drawRect(off, off, size - 2 * off, size - 2 * off)
    else:
        side = int(size * (0.30 ** 0.5))
        off = (size - side) // 2
        p.drawEllipse(off, off, side, side)
    p.end()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)


def run(mode="stub", limit=None, out_dir=OUT, manifest_path=MANIFEST, verbose=True):
    with open(manifest_path, encoding="utf-8") as fh:
        man = json.load(fh)
    av = _load_validator()

    assets = man["assets"][:limit] if limit else man["assets"]
    accepted, rejected, errors = [], [], []

    for a in assets:
        dest = os.path.join(out_dir, a["path"].replace("/", os.sep))
        if os.path.exists(dest):
            # Never silently clobber a generated asset; a re-run should be explicit.
            errors.append("%s already exists — refusing to overwrite" % a["path"])
            continue
        try:
            if mode == "stub":
                stub_image(dest, a["kind"], mode=FORCED_REJECTS.get(a["slot"], "good"))
            else:
                real_image(dest, a["prompt"])
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s: %s" % (a["path"], type(exc).__name__, exc))
            continue

        res = av.validate(dest, a["kind"])
        if res.verdict == av.PASS:
            accepted.append(a["path"])
        else:
            bin_path = os.path.join(out_dir, REJECT_DIR, a["path"].replace("/", "_"))
            os.makedirs(os.path.dirname(bin_path), exist_ok=True)
            shutil.move(dest, bin_path)
            rejected.append((a["path"], a["slot"], res.failures()))

    if verbose:
        print("mode=%s  %d asset(s) from %s" % (mode, len(assets), os.path.basename(manifest_path)))
        print("  accepted %d   rejected %d   errors %d" % (len(accepted), len(rejected), len(errors)))
        for path, slot, why in rejected:
            print("  REJECT %-28s %s" % (slot, "; ".join(w[:70] for w in why[:1])))
        for e in errors:
            print("  ERROR  %s" % e)
        print()
        print("⚠ A stub run tests PLUMBING ONLY — that files land where the rig expects and that the")
        print("  validator's verdict routes them. It says nothing about whether the art is right,")
        print("  because no art was generated.")
    return accepted, rejected, errors



# ── the paid path ────────────────────────────────────────────────────────────────────────
IMAGEGEN = r"C:/Users/prjgn/.codex/skills/.system/imagegen/scripts/image_gen.py"
SECRETS = r"C:/Users/prjgn/AppData/Roaming/ShipBit/WingmanAI/2_0_0/configs/secrets.yaml"

# ⛔⛔ THE MODEL IS PINNED AND THE REASON WOULD HAVE COST A WHOLE BATCH.
#   image_gen.py's own guard, line ~194: "transparent backgrounds are not supported in gpt-image-2,
#   the latest model. Use --model gpt-image-1.5 --background transparent --output-format png".
#   gpt-image-2 is its DEFAULT. Every asset in this pipeline is an attachment that must have a real
#   alpha cutout, and asset_validate rejects anything without one — so accepting the default would
#   have generated 26 opaque images, failed all 26 on the `cutout` rule, and billed for every one.
# ★ The dry run could never have caught this: the stub does not call the API. Reading the tool did.
# ⚠⚠ gpt-image-1.5 IS DEPRECATED — shutdown scheduled 2026-12-01 (flagged by the designer
#   2026-09-27). It is still the default here for ONE measured reason: image_gen.py refuses
#   transparent backgrounds on gpt-image-2, and every asset in this pipeline needs real alpha.
#   So the newer model is better at reference images and cannot do the one thing I require.
# ⇒ Overridable rather than baked in, so the day gpt-image-2 (or its successor) supports
#   transparency this is a flag and not a patch. PICO_IMAGE_MODEL wins if set.
IMAGE_MODEL = os.environ.get("PICO_IMAGE_MODEL", "gpt-image-1.5")
IMAGE_MODEL_SUNSET = "2026-12-01"   # gpt-image-1.5; re-evaluate before this date


def _api_key(path=SECRETS):
    """Read the OpenAI key out of WingmanAI's secrets. Returns the value; never logs it.

    ⚠ Callers pass this into a subprocess ENV, not onto a command line. An API key in argv is
      visible in the process table to anything that can list processes, and this house has a probe
      that prints command lines.
    """
    import re
    with open(path, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
    m = re.search(r"^\s*openai\s*:\s*([^\s#]+)\s*$", txt, re.M | re.I)
    if not m:
        raise RuntimeError("no openai key found in secrets.yaml — refusing to call a paid API blind")
    return m.group(1).strip().strip('"' + "'")


REFERENCE_DIR = os.path.join(HERE, "assets", "reference")

#: Reference images, in the order the prompt preamble names them. The FIRST is always the
#: canonical Pico — style, proportion, scale. The SECOND, when present, is the manufacturer
#: design sheet for the garment being made.
#: ⚠ ORDER IS LOAD-BEARING: role_preamble() writes "Image 1 is..." / "Image 2 is..." to match
#:   this list. Reorder the list without reordering the preamble and every generation is told to
#:   take its silhouette from the wrong sheet — which would look like a style failure, not a
#:   wiring one, and I would go re-prompting instead of re-reading.
REFERENCES = [
    ("pico", "pico_style_sheet_2026-09-27.jpg"),
    ("manufacturer", "pico_asset_sprite_sheet.png"),      # NOT YET SUPPLIED — J is sending it
]


def available_references():
    """-> [(role, path)] for references that actually exist on disk, in declared order.

    ⚠ Returns only what is PRESENT. A missing reference is not an error here — the pipeline is
      designed to run with one, or with none — but the caller must build its preamble from THIS
      list rather than from REFERENCES, or the prompt will describe an Image 2 that was never
      attached. That mismatch is silent: the model simply invents what it was told exists.
    """
    out = []
    for role, name in REFERENCES:
        p = os.path.join(REFERENCE_DIR, name)
        if os.path.exists(p):
            out.append((role, p))
    return out


ROLE_TEXT = {
    "pico": ("is the canonical PICO character reference. Use it ONLY to match the established "
             "rendering style, proportions, materials, lighting, edge treatment and scale. "
             "Do NOT copy any part of the character itself into the output."),
    "manufacturer": ("is the manufacturer design reference. Use it ONLY as the design reference "
                     "for the requested garment — its construction, colourway and weathering. "
                     "Do NOT cut the garment out of it; draw a new clean asset."),
}


def role_preamble(refs):
    """The 'Image N is...' block, generated FROM the references actually attached."""
    if not refs:
        return ""
    lines = []
    for i, (role, _path) in enumerate(refs, start=1):
        lines.append("Image %d %s" % (i, ROLE_TEXT[role]))
    return "\n".join(lines) + "\n\n"


def real_image(dest, prompt, model=IMAGE_MODEL, timeout=300, refs=None, dry_run=False):
    """Generate ONE image. This bills unless dry_run. Raises on anything that is not a written PNG.

    ★ WITH REFERENCES IT USES THE EDIT ENDPOINT, NOT GENERATE. The designer's advice, 2026-09-27:
      "feed the master Pico/reference artwork into the image request so the generations aren't
      trying to rediscover our style from prose each time", assigning each input a role. image_gen
      declares --image with action="append", so several references go in one call.
    ⚠ input-fidelity high is set for the same reason: a reference passed at low fidelity is a mood
      board, not a spec.
    """
    import subprocess
    import tempfile

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = _api_key()
    env["PYTHONIOENCODING"] = "utf-8"

    fd, pf = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(role_preamble(refs) + prompt)
        # ⚠ 'generate' IS A SUBCOMMAND AND OMITTING IT COST NOTHING ONLY BECAUSE I DRY-RAN FIRST.
        #   image_gen.py's top-level parser takes {generate, generate-batch, edit}; every --flag I
        #   read out of its add_argument calls belongs to the SUBPARSER. Without the verb it exits
        #   'invalid choice: gpt-image-1.5' — so the wiring looked right and would have failed on
        #   the first paid call. Reading argparse lines does not tell you which parser owns them.
        verb = "edit" if refs else "generate"
        cmd = [sys.executable, IMAGEGEN, verb,
               "--model", model,
               "--prompt-file", pf,
               "--background", "transparent",
               "--output-format", "png",
               "--size", "1024x1024",
               "--no-augment",          # the manifest prompt is already fully specified
               "--out", dest]
        for _role, _path in (refs or []):
            cmd += ["--image", _path]
        if refs:
            cmd += ["--input-fidelity", "high"]
        if dry_run:
            cmd += ["--dry-run"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    finally:
        try:
            os.unlink(pf)
        except OSError:
            pass

    if dry_run:
        # a dry run writes nothing; the point is that the INVOCATION is accepted
        if r.returncode != 0:
            raise RuntimeError("dry-run rejected: %s" % ((r.stderr or r.stdout or "").strip()[-300:]))
        return r.stdout
    if r.returncode != 0 or not os.path.exists(dest):
        # ⚠ Surface the tool's OWN words. A generic "generation failed" would hide quota, moderation
        #   and model-availability errors, which are three different decisions for a human.
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
        raise RuntimeError("image_gen rc=%s: %s" % (r.returncode, " | ".join(tail[-3:]) or "no output"))
    return dest

def _selftest():
    import tempfile
    fails = []
    try:
        from PySide6.QtGui import QImage  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print("generate_assets selftest: CANNOT TELL — PySide6 unavailable (%s)" % type(exc).__name__)
        return 2
    if not os.path.exists(MANIFEST):
        print("generate_assets selftest: CANNOT TELL — no manifest; run build_manifest.py first")
        return 2

    tmp = tempfile.mkdtemp(prefix="picodry_")
    try:
        acc, rej, err = run(mode="stub", out_dir=tmp, verbose=False)

        if err:
            fails.append("a clean run produced %d error(s): %s" % (len(err), err[:2]))
        if not acc:
            fails.append("nothing was accepted — the accept path never ran")

        # ★ THE CHECK THAT MAKES THE REHEARSAL WORTH RUNNING: exactly the forced set was rejected.
        #   Too few means the reject path is dead; too many means the validator is rejecting art it
        #   should keep, which on the real run would burn money regenerating good images.
        got = {slot for _p, slot, _w in rej}
        want = set(FORCED_REJECTS)
        if got != want:
            fails.append("forced rejects were %s but the run rejected %s — %s"
                         % (sorted(want), sorted(got),
                            "reject path is not firing" if not got - want else "over-rejecting"))

        # every accepted file must actually be on disk where the rig will look for it
        for rel in acc:
            if not os.path.exists(os.path.join(tmp, rel.replace("/", os.sep))):
                fails.append("accepted %s is not on disk at its manifest path" % rel)
                break
        # every rejected file must be OUT of the skins tree, not left where the rig would load it
        for rel, _slot, _w in rej:
            if os.path.exists(os.path.join(tmp, rel.replace("/", os.sep))):
                fails.append("rejected %s is still sitting in the skins tree" % rel)
                break

        # a second run over the same directory must refuse rather than overwrite
        _a2, _r2, e2 = run(mode="stub", out_dir=tmp, verbose=False)
        if not e2:
            fails.append("a re-run silently overwrote existing assets instead of refusing")

        print("generate_assets selftest: %s (%d accepted, %d rejected, forced=%s)"
              % ("PASS" if not fails else "FAIL", len(acc), len(rej), sorted(want)))
        for f in fails:
            print("   -", f)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if fails else 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["stub", "real"], default="stub")
    ap.add_argument("--i-mean-it", action="store_true",
                    help="required with --mode real; without it a real run refuses")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.mode == "real" and not a.i_mean_it:
        print("REFUSING: --mode real bills a paid API. Add --i-mean-it if that is what you want.")
        return 2
    run(mode=a.mode, limit=a.limit, out_dir=a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
