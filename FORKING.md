# Building SC Toolbox from a fork

This file says how to run SC Toolbox from source and how to build its installer from a fresh
clone. It was checked on 2026-10-07 by cloning this branch into an empty folder on Windows 11
and doing each step there. Every step says whether it was run in that check. `build/RELEASING.md`
describes how the project's own releases are built and published.

## What you need

To run from source:

- Windows, Git, and Python. The check used Python 3.13.1. `pyproject.toml` asks for 3.10 or
  newer; other versions were not checked.

To build the installer, also:

- **Inno Setup 6.** `build_installer.bat` looks for `ISCC.exe` in the two Program Files folders,
  in `%LOCALAPPDATA%\Programs\Inno Setup 6`, and on the PATH, and stops at once if it is not
  there. The Velopack build never runs it.
- **.NET SDK.** The check used SDK 10.0.200. The launcher and the installer target .NET 8 and
  are published self-contained. Other SDK versions were not checked.
- **`vpk`, the Velopack command-line tool, version 0.0.1298.** It is the same version as the
  Velopack package the launcher uses. It was already installed for the check;
  `dotnet tool install -g vpk --version 0.0.1298` is the usual way to get it and was not run.
  `vpk` is made for .NET 9. The build script sets `DOTNET_ROLL_FORWARD=Major` so it runs on a
  newer one.
- `curl`, PowerShell and `robocopy`, which come with Windows.
- An internet connection, and about 16 GB of free disk. After the check the clone folder held
  13.4 GB of build output, next to 1.1 GB of git history and a 1.0 GB Python environment.

## Run from source

    git clone <your fork>
    cd <the folder>
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    .venv\Scripts\python skill_launcher.py 100 100 500 550 0.95 nul

Checked: the environment installs from `requirements.txt`, and all 17 tools' entry scripts import
in it (the same test the build runs, `build/staging_import_test.py`, pointed at the source folder).

Not checked: opening the launcher window. The last line is the command `LAUNCH.bat` runs; no
window was opened during the check. `LAUNCH.bat` and `INSTALL_AND_LAUNCH.bat` were not run.

From source some parts are missing because their files are not in git: see the next section.
Mining Signals also has no bundled Tesseract or PaddleOCR until a build puts them in place. How
each tool behaves without its missing files was not checked beyond the import test.

## The files git does not hold

Five sets of files are kept out of git because they are large or are art. The build needs them.

| What | Size in the 3.0.0 package | Where the build reads it |
|---|---|---|
| The props Pico holds (586 files) | 36.5 MB | `tools/Pico/out/snap_props/` |
| Pico's default outfit pack `o08.tar.xz` | 12.7 MB | `tools/Pico/packs/` |
| The SuitMk2 sound classifier (YAMNet, 2 files) | 15.1 MB | `tools/SuitMk2/models/yamnet/` |
| The two SuitMk2 characters and their manifest | 327.6 MB | the folder `SUITMK2_MODELS_SRC` names |
| The two SuitMk2 voices (4 files) | 127.0 MB | the folder `SUITMK2_VOICES_SRC` names |

Every published release package holds all five. Download `SC_Toolbox-3.0.0-full.nupkg` (1.8 GB)
from the releases page of https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2, then:

    python build\assets_from_release.py <path to>\SC_Toolbox-3.0.0-full.nupkg

It copies the five sets into place, checks the two character files against their manifest, and
prints two `set` lines to use before the build. It leaves alone any file that already exists.

Checked: the script, on a copy of the 3.0.0 package taken from the PC that built it. Downloading
the package from GitHub was not checked.

Without the package:

- **Pico's props have no substitute.** The build script stops with "tools\Pico\out\snap_props
  is missing" (read in the script; not run without them).
- The outfit pack is downloaded by the build from `https://pico-pals.pages.dev/packs` when it is
  not in `tools/Pico/packs`. That download was checked on its own: the file matched the size and
  sha256 in `packs.json`.
- The characters and voices can be left out with `set SUITMK2_ALLOW_NO_MODELS=1` and
  `set SUITMK2_ALLOW_STOCK_VOICES=1`. Building that way was not checked.
- The build script has no check for the sound classifier. Read from the script, not run: without
  it the package is built and SuitMk2 has no sound classifier. `tools/SuitMk2/models/yamnet/SOURCE.json` names where the
  model came from; fetching it from there was not checked.

The props, outfits, voices and characters are the original project's work, and Pico is a Star
Citizen character. `LICENSE` says what the MIT part covers and what it does not.

## Build the installer

Open a Command Prompt, run the two `set` lines the script above printed, then run the build by
its full path:

    set "SUITMK2_MODELS_SRC=<clone>\build\release_assets\suitmk2_models"
    set "SUITMK2_VOICES_SRC=<clone>\build\release_assets\suitmk2_voices"
    <clone>\build\_run_build.bat

Checked: this finished with exit code 0 in 15 minutes. The first run downloads more and takes
longer. It left in `build\Releases`:

    SC_Toolbox-3.0.0-full.nupkg     1.72 GB   the package the updater reads
    SC_Toolbox-win-Setup.exe        1.72 GB   Velopack's own installer
    SC_Toolbox-win-Portable.zip     1.72 GB
    RELEASES, releases.win.json, assets.win.json

A fresh clone has no earlier package in `build\Releases`, so no delta package is made.

The installer new users see is a second step, and it carries the Setup.exe from the first:

    dotnet publish build\installer_ui\SC_Toolbox_Installer.csproj -c Release -o build\installer_ui\publish
    python build\release_check.py

Checked: both ran. The first wrote `build\installer_ui\publish\SC_Toolbox_Setup.exe` (1.96 GB)
and the second answered READY.

Not checked: running either installer, the installed program, or an update from one version to
the next. Nothing was installed during the check. Nothing is code-signed.

## What the build downloads

- `python-3.14.0-embed-amd64.zip` from python.org and `get-pip.py` from bootstrap.pypa.io.
- From PyPI, into that Python: PySide6, requests, pynput, mss, pytesseract, Pillow, cryptography,
  onnxruntime, numpy, scipy, onnx, piper-tts, sounddevice, faster-whisper, and what they need.
  Only minimum versions are given, so you get the newest of each.
- Tesseract. When `C:\Program Files\Tesseract-OCR` exists it is copied from there, which is what
  happened in the check. Otherwise the build downloads the UB-Mannheim installer (5.4.0.20240606)
  and runs it silently into `build\_tess_tmp`. That path was not checked.
- `python-3.13.1-embed-amd64.zip`, and into it paddlepaddle 3.0.0, paddleocr, numpy and Pillow,
  for Mining Signals' second text reader.
- Pico's default outfit pack, when it is not already in `tools/Pico/packs`.
- The NuGet packages the launcher and the installer need, fetched by `dotnet`.

## How a build from a clone differs from the published 3.0.0

The package built in the check was compared with the published one, file by file.

- It has 19,570 files where the published one has 38,405. The 18,835 it lacks (117.9 MB) are
  working files that were lying in the folder 3.0.0 was built from: training pictures, screen
  recordings and notes under `tools/Mining_Signals`, and a 41.7 MB data file under
  `skills/Cargo_loader/datamine`. No code that runs was found to read them.
- It has no file the published one lacks.
- 460 files differ only in their line endings. Git for Windows checks text files out with
  Windows line endings by default; the published package was built from files with Unix ones.
- Apart from the two programs the build makes and one log file a build check writes, the
  only other files that differ are three under `shared/shopping` whose opening comments were
  shortened on this branch.

## Before you give your build to anyone

A build made as above is still the original program under the original name. Change these first.

- **Where updates come from.** `build/launcher/Program.cs`, `UPDATE_REPO_URL` (line 45), and
  `shared/update_checker.py`, `GITHUB_REPO` (line 28). Both point at
  `ScPlaceholder/SC-Toolbox-Beta-V2`. Left as they are, your users' copies look for updates in
  the original project's releases.
- **The program's id and name.** `build/build_installer.bat`, in the `vpk pack` command:
  `-u "SC_Toolbox"`, `--packTitle "SC Toolbox"` and `--packAuthors "ScPlaceholder"`. And
  `build/installer_ui/InstallPaths.cs`: `APP_DIR_NAME` and `UNINSTALL_KEY` (lines 25 and 26).
  With the same id your build installs into the same folder, `%LOCALAPPDATA%\SC_Toolbox`, and
  uses the same uninstall entry as the original. The package file names
  (`SC_Toolbox-<version>-full.nupkg`, `SC_Toolbox-win-Setup.exe`) follow the id, and
  `build/release_check.py`, `build/fix_emptied_files.py` and the installer's project file name
  them. Building under a different id was not checked.
- **The version.** `pyproject.toml`, `version`. It is the only place: the build and the
  installer read it from there.
- **Where Pico's outfits come from.** `PACKS_URL` in `tools/Pico/pico/packs.py` and
  `PICO_PACKS_URL` in `build/build_installer.bat` point at the original project's pack site.
- **Where settings are kept.** `shared/user_settings.py` keeps them in `~/.sctoolbox`. A fork
  that keeps this shares its settings folder with the original on the same PC.
