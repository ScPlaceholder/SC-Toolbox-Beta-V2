# SC Toolbox — Build Staging Asset-Pruning Guide

**Audience:** an automated agent (Claude) preparing a *staged build* of the SC
Toolbox for distribution. **You have no prior context — follow this exactly.**

## Goal

Shrink the **staged/shipped** build by removing **training, evaluation,
capture, and debug** assets that are NOT loaded at runtime, while keeping
**every runtime asset**. End users only need runtime assets. Training data
lives in the dev repo and is used only for retraining — it does not ship.

## Rule #0 — never prune the dev tree, only the staging COPY

Operate **only** on the staging/build output directory (e.g.
`...\SC_Toolbox_Beta_V1.2\build\staging\...`). The dev working tree and its
git repo must keep ALL training data — that is the source of truth and must
never lose data. If you are unsure which tree you are in, STOP and ask.

## Rule #1 — a folder's NAME does not tell you if it ships

The single most important warning: **some directories named like training
data are actually runtime assets.** Never delete a directory by name alone.
Verify with the procedure in "How to classify any directory" below.

> Canonical trap (real example): `tools/Mining_Signals/training_data_blacklist/`
> sounds like training data but is loaded **at runtime** to build icon/ghost
> NCC templates on every signature read. Deleting it corrupts reads.

---

## Mining_Signals (`tools/Mining_Signals/`) — AUTHORITATIVE lists

### KEEP — runtime; deleting these breaks the tool
- **`training_data_blacklist/`** — ⚠️ misleading name, RUNTIME. Loaded by
  `ocr/sc_ocr/api.py` (`_drop_blacklisted_signature_glyphs` →
  `_build_signature_blacklist_templates`, `BLACKLIST_DIR.rglob("*.png")`,
  ~line 2645) and `ocr/glyph_gate.py`. Keep the whole folder, PNGs included.
- **All `*.onnx`** — the CNN/CRNN model weights.
- **`ocr/sc_templates/*.npz`** and **`ocr/sc_templates/regions.json`** — baked
  templates/labels loaded at runtime. (These are `.npz`/`.json`, not loose
  pictures, so a "delete pictures" sweep won't touch them — but don't delete
  the folder.)
- **`world_model_region2.json`** and any **calibration `*.json`**.
- **`tesseract/`** — the bundled OCR engine.
- **All `*.py`** source.

### DELETE — training / eval / capture / debug only (not runtime)
- `training_data_panels/`        — labeled accuracy-gate panels (eval/retrain)
- `training_data_crnn/`          — CRNN training crops
- `_v3_icon_staging_rgb/`        — referenced ONLY by `ocr/train_signal_rgb*_v3.py`
- `debug_glyphs/`                — runtime debug dumps (regenerated on demand)
- `debug_*.png`, `_dbg_*.png`    — loose debug images
- `live_samples/`                — raw capture samples
- `panel_finder_recording/`      — capture recordings
- `ocr/sc_templates/labels_debug/` — debug tiles
- `*.jsonl` (e.g. `scan_records.jsonl`, `signature_records.jsonl`) — telemetry logs
- `*.csv` eval outputs (e.g. `failure_profile.csv`)

> Net effect: nearly all the build's bulk (thousands of PNGs) is in
> `training_data_panels/`, `training_data_crnn/`, `_v3_icon_staging_rgb/`,
> `debug_glyphs/`, and `live_samples/` — all safe to delete.

---

## How to classify ANY directory (use for every OTHER tool in the toolbox)

You only have authoritative lists for Mining_Signals. For every other tool,
**do not guess** — prove a directory is dev-only before deleting it.

For a candidate directory `DIR` inside a tool, run BOTH checks:

**Check A — who references the directory name?**
```bash
grep -rniE "DIR" --include=*.py <tool_dir> | grep -viE "train|extract|eval|test|harness|debug|dump|profile"
```
- If this prints a hit inside a **runtime module** (`api*.py`, `*reader*.py`,
  `*finder*.py`, `app.py`, `*daemon*.py`, `*client*.py`, anything imported by
  the running app) → **KEEP `DIR`.**
- If it prints nothing (only `train_*`/`eval_*`/`test_*`/`*debug*` scripts
  referenced it) → `DIR` is dev-only → safe to delete.

**Check B — what do the runtime image loaders read?**
```bash
grep -rniE "Image\.open|cv2\.imread|rglob\(.*\.png|glob\(.*\.png|listdir" --include=*.py <tool_dir> | grep -viE "train|extract|eval|test|debug|dump|profile"
```
Trace each surviving hit to the directory it reads. **Those directories are
runtime-critical — KEEP them**, regardless of name.

### General heuristics (apply only after Checks A/B confirm)
- **KEEP (runtime):** `*.onnx`, `*.pt`, `*.tflite`, `*.npz`, model/calibration
  `*.json`, bundled binaries (`tesseract/`, `ffmpeg`, etc.), and any image dir
  a runtime loader globs.
- **DELETE (dev-only):** dirs named `training_data*`, `*_staging*`, `*dataset*`,
  `debug*`, `*_samples`, `*recording*`; plus `*.jsonl` telemetry and `*.csv`
  eval outputs — **but only when Check A/B come back clean.**

---

## Final safety checklist (run every time)
1. ☐ Confirm you are in the **staging copy**, not the dev tree (Rule #0).
2. ☐ For each tool, run **Check A + Check B** before deleting any asset dir.
3. ☐ Never delete a dir by name alone (remember `training_data_blacklist`).
4. ☐ Keep every `*.onnx` / `*.npz` / model-or-calibration `*.json` / bundled binary.
5. ☐ After pruning, **launch the built tool and exercise its main path.** For
   Mining_Signals: do one in-game scan and confirm the signature value and the
   SCAN RESULTS panel still read, and the glyph viewer shows no stray icon
   bits. If anything errors on a missing file, that file was runtime — restore it.
6. ☐ The dev repo (all training data) stays intact and untouched.
