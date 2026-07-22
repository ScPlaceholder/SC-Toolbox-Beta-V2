## What's New in v2.2.6

### SC_OCR — purpose-built HUD reader for Mining Signals

Mining Signals previously relied on stock Tesseract, which struggled with the SC HUD's sparse digits, anti-aliased glyphs, and varying background luminance. v2.2.6 introduces **SC_OCR**: a CNN-based reader trained on actual in-game captures.

- **Adaptive (locally-windowed) thresholding** — handles bright vs dark backgrounds without hand-tuning
- **Position-based row finder with lock cache** — once a row is found, subsequent frames lock onto it for stability and speed
- **Multi-frame averaging** — smooths out transient OCR noise from animated panels
- **Glyph-level confidence** — per-digit classification scores, with an optional live "Glyph Reader" debug view
- **Self-curating training pipeline** — captures, auto-labels (with consensus voting), quarantines contaminated samples, and stages new data for review

You don't need to do anything to use SC_OCR — it ships pre-trained inside Mining Signals.

### Privacy hardening

- File logs and crash dumps are scrubbed of home-directory paths, usernames, hostnames, IPs, MAC addresses, emails, and auth tokens at write time. Console output is unchanged so local debugging is unaffected.
- Build pipeline strips PyTorch ONNX stack-trace metadata and pip-generated wrapper shebangs that previously carried the build-machine username.
- Nothing personally identifying leaves your machine in a debug or crash report.

### Cleaner installer

~150 MB smaller than v2.2.5 — training data, training scripts, and dev-only debug artifacts no longer staged. End users get a pure runtime.

### Other fixes

- Launcher BAT files and audit/training scripts now resolve their python path via `%LOCALAPPDATA%` instead of a hardcoded user path — portable across machines.
- Generalized PII gate in the build pipeline catches any home-directory path leak in the staged config.

---

**Download**: `SC_Toolbox_Setup_2.2.6.exe` below (~830 MB)
**Discord**: https://discord.gg/D3hqGU5hNt
**Issues**: https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/issues
