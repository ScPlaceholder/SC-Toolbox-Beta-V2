## What's New in v2.2.9

This release lands a large batch of **Mining Signals** scanner work — a new RGB-native OCR stack, a multi-anchor HUD panel finder, and a deeper read-stabilization layer that keeps the overlay from flickering or publishing a wrong value.

### Mining Signals OCR — RGB CNN + RGB CRNN

The OCR that reads both panels — SCAN RESULTS (MASS / RESISTANCE / INSTABILITY) and the signature value — now runs a strict priority cascade of independent recognizers and accepts the first that clears a confidence gate.

- **RGB CNN (primary)** — a digit CNN trained directly on live in-game *colour* crops instead of grayscale, so it stops clipping 9s and other thin-stroke digits the old luma path lost. Ships with a per-channel **inverted variant** (`signal_rgb_inv`) for low-luminance panels; validated to 100% on its train/validation split.
- **RGB CRNN (secondary)** — a full-sequence Conv + BiLSTM + CTC reader, also RGB-trained, for whole-value and mineral-name reads. Used when the RGB CNN voters disagree but their glyph geometry still lines up.
- **Grayscale CNN (backup)** — the v2.2.6 reader, kept as a defensive layer for unusual lighting.
- **Tesseract (last resort)** — only fires when every neural voter fails its gate.

**Dual-polarity voting** underpins the CNNs: each segmented digit is read by two zero-shared-weight models on opposite polarities. Their errors decorrelate, so agreement is trusted outright and *disagreement itself* becomes the signal to defer — which is what finally kills the SC-font 5-vs-6 flip (e.g. 11,565 ⇄ 11,655). High-confidence RGB reads short-circuit the rest of the cascade: fewer dropped digits in low-contrast frames, faster reads overall.

### HUD panel finding — multi-anchor localization

Finding the panels on a moving, particle-occluded HUD no longer hangs on a single fragile detector — several independent anchors are matched and cross-checked:

- **Signature panel** — multi-scale NCC against the location-pin **icon**, with templates pre-built at 13 widths (12–72 px). A CNN re-rank rejects digit clusters that score like the icon, and a leftmost-peak rule filters the `9` / `,0` glyph shapes that NCC-correlate against it.
- **SCAN RESULTS panel** — multi-scale NCC against the rendered **"SCAN RESULTS" title**, matched on a max-of-RGB channel that survives the HUD's chromatic aberration.
- **Label rows** — per-row NCC for `MASS:` / `RESISTANCE:` / `INSTABILITY:`, anchored MASS-first with FFT-accelerated correlation, plus redundant projection-band and fixed-proportion fallbacks.
- **Chrome-line detection** — locates the thin horizontal HUD separator lines bracketing the data area, so the value rows can still be placed when the title itself is occluded by snow or particle effects.
- **Multi-detector HUD tracker** — independent geometry, contour, NCC and CNN detectors vote on the icon/panel position, backed by a geometric model of the panel's fixed proportions so one solid anchor can place everything else.

The upshot: four independent routes to the same three rows, any one of which can carry a frame alone.

### HUD tracking & read stabilization

A stack of consensus, hysteresis and locking layers sits on top of the engines — each one added in response to a specific real-world flicker or wrong read:

- **Rolling per-field consensus** — a value never reaches the overlay without majority agreement across a 5-frame buffer, cross-checked against a parallel crop-fingerprint buffer.
- **Field-value locking** — once a field reads identically across all 5 frames *and* its crop NCC holds ≥ 0.85, the value locks and OCR is skipped for it until the crop fingerprint drifts.
- **Signal-value consensus** — a 6-frame buffer that now requires 4 consecutive identical reads before swapping a value (raised from 2, which was letting the 5-vs-6 ambiguity flip the signal).
- **Lexicon tiebreaker** — known-good signature values are preferred over off-table candidates even before majority is reached, killing the dominant flicker pattern outright.
- **Anchor-gate hysteresis** — a strict score floor to *enter* the "panel present" state and a relaxed floor to *stay* in it, so frame-to-frame score wobble no longer drops the overlay back to "Scanning…".
- **Panel stabilization** — sub-pixel HUD jitter is absorbed by multi-frame averaging and a frozen-panel tracker, and the scan / break bubbles fingerprint their content so unchanged data never tears down and rebuilds the widget tree.

Net effect: the overlay holds steady while you mine, and it will show nothing before it shows a wrong number.

### Signature scanner — false-positive fix

The signature finder no longer mistakes the green scan-direction arrow for the signature's location-pin icon. A temporal stability gate now requires the icon anchor to be detected consistently — at least three recent frames agreeing within a few pixels — before a signature is emitted, so a transient mis-detection no longer spawns a phantom signature when the icon isn't actually on screen.

### Glyph reviewer — drag-select + augmentation cascade

The training-data review tool (`scripts/review_glyphs.py`) now supports:

- **Bounding-box drag-select** — hold the mouse and sweep across multiple glyphs to select them in one motion, instead of click-by-click.
- **Augmentation cascade** — when you move an original sample to a different label, every `aug_<stem>_*.png` sibling moves with it automatically. No more orphaned augmentations stuck in the wrong class.

### Segmentation & binarization fixes

- **Multi-recipe binarization** — wide signature spans are now thresholded with several adaptive recipes in parallel; the recipe whose segment count matches the expected glyph count wins. Replaces the previous greedy lowest-ink split that occasionally merged adjacent digits.
- **Equal-width span splitter** — when a wide blob still survives binarization, it splits along equal widths instead of seeking the lowest-ink valley, which was unreliable on anti-aliased strokes.
- **Comma-mask thresholds proportional to glyph height** — the comma filter now scales with the upscaled crop's actual height instead of native-pixel constants. Stops bleeding through after Lanczos upscale.
- **Stable-signal hysteresis** — new `_accept_signal_value()` gate prevents single-frame OCR jitters from being published to the chart bubble.

### Bundled-runtime fixes

- `scipy` is now properly declared in the bundled Mining_Signals requirements (was silently missing in the prior build, which broke import on first scan in clean installs).
- Paddle sidecar's pip install now uses `--only-binary=:all:` to skip the `python-bidi 0.6.9` sdist that requires Rust/maturin to build on Python 3.13. Falls back to the `0.6.7` cp313 wheel.
- The installer's internal version constant now tracks the shipped version, so the upgrade check no longer misfires on an older install.

---

**Download**: `SC_Toolbox_Setup_2.2.9.exe` below (~1.7 GB; bundled installer with custom progress UI)
**Discord**: https://discord.gg/D3hqGU5hNt
**Issues**: https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/issues
