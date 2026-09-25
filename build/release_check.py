"""Pre-publish check for an SC Toolbox release. Read-only: it never builds or uploads.

WHY: v2.3.0 and v2.3.1 were published with only the branded SC_Toolbox_Setup_<ver>.exe.
The Velopack update feed (releases.win.json, RELEASES, the full/delta .nupkg) was left
off, so the launcher's auto-update (build/launcher/Program.cs, GithubSource) found nothing
and swallowed the error: users on 2.2.16 were never offered 2.3.x. This script makes that
state impossible to call "ready".

    python build/release_check.py              # check build/Releases + the custom installer
    python build/release_check.py --selftest

Exit codes: 0 READY, 1 NOT READY (a required piece is missing or wrong), 2 cannot tell.
On READY it prints the exact asset list to attach to the GitHub release.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def pyproject_version(root: Path) -> str:
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"',
                  (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else ""


def file_version(path: Path) -> str:
    """Windows FileVersion of an .exe via PowerShell, or '' if unreadable."""
    if os.name != "nt" or not path.exists():
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item -LiteralPath '{path}').VersionInfo.FileVersion"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return out
    except Exception:
        return ""


def check(root: Path, releases: Path, installer: Path, read_exe_version=file_version):
    """Return (errors, warnings, assets). errors block a release; warnings do not."""
    errors, warnings = [], []
    v = pyproject_version(root)
    if not v:
        return ["pyproject.toml has no version line"], [], []

    iss = root / "build" / "SC_Toolbox_Installer.iss"
    if iss.exists() and f'"{v}"' not in iss.read_text(encoding="utf-8", errors="replace"):
        warnings.append(f".iss MyAppVersion is not {v} (Inno path only; the custom installer ignores it)")
    xaml = root / "build" / "installer_ui" / "MainWindow.xaml"
    if xaml.exists() and v not in xaml.read_text(encoding="utf-8", errors="replace"):
        warnings.append(f"MainWindow.xaml display strings are not {v} (cosmetic; overwritten at runtime)")

    feed = ["RELEASES", "releases.win.json", f"SC_Toolbox-{v}-full.nupkg"]
    for name in feed:
        if not (releases / name).exists():
            errors.append(f"missing Velopack feed file: {releases / name}")
    delta = releases / f"SC_Toolbox-{v}-delta.nupkg"
    if not delta.exists():
        warnings.append(f"no delta package ({delta.name}): existing users download the full package "
                        "once. Expected only when no previous Velopack release was in build/Releases.")
    if not (releases / "assets.win.json").exists():
        warnings.append("assets.win.json missing (v2.2.16 shipped it; the feed works without it)")

    rj = releases / "releases.win.json"
    if rj.exists():
        try:
            assets = json.loads(rj.read_text(encoding="utf-8")).get("Assets") or []
            versions = {a.get("Version") for a in assets}
            if v not in versions:
                errors.append(f"releases.win.json does not list version {v} (lists {sorted(versions)})")
            for a in assets:
                fn, want = a.get("FileName"), (a.get("SHA256") or "").upper()
                p = releases / fn if fn else None
                if a.get("Version") == v and p and p.exists() and want:
                    import hashlib
                    h = hashlib.sha256()
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 22), b""):
                            h.update(chunk)
                    if h.hexdigest().upper() != want:
                        errors.append(f"{fn} does not match the SHA256 in releases.win.json "
                                      "(stale or re-built package; re-run vpk pack)")
        except Exception as e:
            errors.append(f"releases.win.json unreadable: {e}")

    vp_setup = releases / "SC_Toolbox-win-Setup.exe"
    if not installer.exists():
        errors.append(f"custom installer not built: {installer}")
    else:
        ev = read_exe_version(installer)
        if ev and not ev.startswith(v):
            errors.append(f"custom installer FileVersion is {ev}, expected {v} (stale stage-2 build)")
        elif not ev:
            warnings.append("could not read the custom installer's FileVersion (not verified)")
        if vp_setup.exists() and installer.stat().st_mtime < vp_setup.stat().st_mtime:
            errors.append("custom installer is OLDER than the Velopack setup it embeds: stage 2 was "
                          "built before stage 1 finished, so it carries the previous payload")

    assets = [str(releases / n) for n in ("RELEASES", "releases.win.json", "assets.win.json",
                                          f"SC_Toolbox-{v}-full.nupkg", f"SC_Toolbox-{v}-delta.nupkg")
              if (releases / n).exists()]
    assets.append(f"{installer} -> SC_Toolbox_Setup_{v}.exe")
    return errors, warnings, assets


def main(argv):
    if "--selftest" in argv:
        return selftest()
    root = ROOT
    releases = root / "build" / "Releases"
    installer = root / "build" / "installer_ui" / "publish" / "SC_Toolbox_Setup.exe"
    try:
        errors, warnings, assets = check(root, releases, installer)
    except Exception as e:
        print(f"release_check: CANNOT TELL - {e}")
        return 2
    v = pyproject_version(root)
    for w in warnings:
        print(f"  warn  {w}")
    for e in errors:
        print(f"  FAIL  {e}")
    if errors:
        print(f"release_check: NOT READY for v{v} ({len(errors)} blocking)")
        return 1
    print(f"release_check: READY for v{v}. Attach ALL of these to the GitHub release:")
    for a in assets:
        print(f"    {a}")
    return 0


def selftest():
    import tempfile
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "build" / "installer_ui" / "publish").mkdir(parents=True)
        (root / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
        rel = root / "build" / "Releases"
        rel.mkdir()
        inst = root / "build" / "installer_ui" / "publish" / "SC_Toolbox_Setup.exe"
        good_ver = lambda p: "9.9.9.0"

        # the v2.3.x state: installer only, no feed
        inst.write_bytes(b"MZ")
        e, w, a = check(root, rel, inst, good_ver)
        case("installer-only release (the 2.3.x mistake) is NOT READY",
             any("feed file" in x for x in e))

        (rel / "RELEASES").write_text("x")
        (rel / "SC_Toolbox-9.9.9-full.nupkg").write_bytes(b"PK")
        (rel / "releases.win.json").write_text(json.dumps({"Assets": [{"Version": "9.9.9"}]}))
        (rel / "SC_Toolbox-win-Setup.exe").write_bytes(b"MZ")
        os.utime(rel / "SC_Toolbox-win-Setup.exe", (1, 1))
        e, w, a = check(root, rel, inst, good_ver)
        case("full feed + fresh installer is READY", e == [])
        case("missing delta is a warning, not a blocker", any("delta" in x for x in w) and not e)

        (rel / "releases.win.json").write_text(json.dumps({"Assets": [{"Version": "9.9.8"}]}))
        e, w, a = check(root, rel, inst, good_ver)
        case("feed listing the wrong version is NOT READY", any("does not list" in x for x in e))
        (rel / "releases.win.json").write_text(json.dumps({"Assets": [{"Version": "9.9.9"}]}))

        e, w, a = check(root, rel, inst, lambda p: "2.3.1.0")
        case("stale custom installer version is NOT READY", any("FileVersion" in x for x in e))

        os.utime(inst, (1, 1))
        os.utime(rel / "SC_Toolbox-win-Setup.exe", None)
        e, w, a = check(root, rel, inst, good_ver)
        case("installer built before its payload is NOT READY", any("OLDER" in x for x in e))
    print("release_check selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
