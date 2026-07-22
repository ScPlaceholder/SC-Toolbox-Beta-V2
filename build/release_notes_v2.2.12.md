## What's New in v2.2.12

The proactive fix for the "scanner doesn't work" failure mode reported on v2.2.10 — and surfaced clearly by v2.2.11's new diagnostic logging.

### The actual fix

The Mining Signals OCR pipeline depends on `onnxruntime`, which is a native C++ library that requires the Microsoft Visual C++ Runtime (`vcruntime140.dll` / `msvcp140.dll`) to load. On Windows installs that don't have the VC++ Redistributable installed — clean Windows installs, Windows N editions, locked-down corporate machines — `onnxruntime` can't load, the scanner's CNN voters all silently report `unavailable`, and the scanner just doesn't work. The pre-v2.2.11 builds gave the user no clue why.

v2.2.12 fixes this two ways:

- **Installer now bundles VC++ Redistributable.** The custom-UI installer (`SC_Toolbox_Setup_2.2.12.exe`) bundles Microsoft's official `vc_redist.x64.exe` and silent-installs it before installing the app — but only if the runtime isn't already present (registry-checked). Users who already have VC++ on their machine (the vast majority) see no extra friction. Users who don't get a one-time UAC prompt for the runtime install, after which the scanner works.
- **App detects missing runtime at startup.** When Mining Signals launches, it sanity-checks that `onnxruntime` can load. If it can't, it shows a dialog explaining what's missing and where to download the fix, instead of letting the scanner silently fail. This is the safety net for users whose VC++ install somehow gets removed after install (rare, but possible).

### What this means for you

- **Fresh installs of 2.2.12**: the scanner just works. The installer takes care of the dependency.
- **Existing installs that were silently broken**: launch SC Toolbox on 2.2.12 (auto-update will deliver it) and a clear dialog will tell you what to install if anything's still wrong. The link goes straight to Microsoft's download page.
- **Working installs**: no change to the user experience. The dependency check passes silently.

---

**Download**: `SC_Toolbox_Setup_2.2.12.exe` below (~1.75 GB; bundled installer with custom progress UI; ~25 MB larger than 2.2.11 because it now carries `vc_redist.x64.exe`)
**Discord**: https://discord.gg/D3hqGU5hNt
**Issues**: https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/issues
