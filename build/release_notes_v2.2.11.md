## What's New in v2.2.11

A small follow-up to v2.2.10.

v2.2.10 fixed the signature scanner's `name 'field' is not defined` crash, but a subset of users reported the scanner still failing afterwards — the icon voter reporting both the RGB and gray CNNs as `unavailable` with no further detail. The underlying cause was a silent-exception pattern in the voter's lazy-load: the real error (onnxruntime failing to import, the ONNX model refusing to open, or the helper import raising) was logged only at `DEBUG` level, so user crash logs surfaced the symptom but never the cause.

### What changed

- **Icon voter logs CNN session failures at `WARNING`.** When the RGB or gray CNN voter can't load for any reason — bad ONNX runtime, missing dependency, corrupted model file, AV interference — the exception type and message now land in the crash log. The next user report from this failure mode will identify the actual culprit so the underlying issue can be fixed (most likely candidates: missing Visual C++ Redistributable, a CPU without AVX2 support, antivirus blocking the ONNX runtime DLL).
- **Build-time validation expanded.** The installer build now validates the full ensemble of Mining Signals model files (17 `.onnx` + `.onnx.data` files plus `furore_templates.npz`) and the `scipy` dependency, instead of only the HUD digit CNN. Catches "model file silently missed from the installer" at build time instead of letting the failure ship.

If the v2.2.10 signature scanner works for you, you'll see no difference in v2.2.11 — the new logging only fires on the failure path that was already broken silently.

If the v2.2.10 signature scanner is still failing for you, this release does not fix it directly, but triggering the crash dialog and sharing the bottom of the log will now produce a `WARNING` line naming the root cause so a real fix can ship in the next release.

---

**Download**: `SC_Toolbox_Setup_2.2.11.exe` below (~1.7 GB; bundled installer with custom progress UI)
**Discord**: https://discord.gg/D3hqGU5hNt
**Issues**: https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/issues
