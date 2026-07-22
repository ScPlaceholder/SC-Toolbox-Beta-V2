@echo off
setlocal enabledelayedexpansion
title SC_Toolbox — Build Installer
color 0E

:: =====================================================================
::  SC_Toolbox Installer Builder
::
::  Prerequisites:
::    - Internet connection (downloads Python embeddable + get-pip.py)
::    - Inno Setup 6 installed (iscc.exe on PATH, or edit ISCC below)
::
::  What this script does:
::    1. Downloads Python 3.12 embeddable package
::    2. Bootstraps pip and installs runtime dependencies
::    3. Stages only the runtime source files (no tests, caches, dev tools)
::    4. Runs Inno Setup to produce SC_Toolbox_Setup.exe
:: =====================================================================

set "ROOT=%~dp0.."
set "BUILD=%~dp0"
set "STAGE=%BUILD%staging"

:: Build mode — first arg picks the installer toolchain.
::   (empty / inno)  → Inno Setup wizard installer (default, legacy)
::   velopack         → Velopack pack/installer with delta auto-updates
:: The staging logic is identical for both; only Step 8 diverges.
set "BUILD_MODE=%~1"
if /I "%BUILD_MODE%"=="velopack" (set "BUILD_MODE=velopack") else (set "BUILD_MODE=inno")
:: Python 3.14 to match the dev-machine runtime. Bumped from 3.12 so
:: the install behaves identically to local: same interpreter, same
:: import speed characteristics, same wheel set. If a future dep drops
:: 3.14 wheels, fall back to 3.13.x — every dep we ship currently has
:: 3.14 wheels on PyPI as of April 2026.
set "PYTHON_VER=3.14.0"
set "PYTHON_ZIP=python-%PYTHON_VER%-embed-amd64.zip"
set "PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VER%/%PYTHON_ZIP%"
set "GETPIP_URL=https://bootstrap.pypa.io/get-pip.py"
set "TESSERACT_URL=https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
set "TESSERACT_INSTALLER=tesseract-setup.exe"

:: Inno Setup compiler — check common install locations
set "ISCC="
for %%D in (
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    "C:\Program Files\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if exist %%D set "ISCC=%%~D"
)
if not defined ISCC (
    where iscc >nul 2>&1
    if !errorlevel!==0 (
        for /f "delims=" %%P in ('where iscc') do set "ISCC=%%P"
    ) else (
        echo  [!] Inno Setup 6 not found. Install from https://jrsoftware.org/isinfo.php
        echo      or add iscc.exe to PATH.
        goto :fail
    )
)

echo.
echo  =============================================
echo   SC_Toolbox — Build Installer
echo  =============================================
echo.

:: ── Step 1: Clean previous build ──
:: rmdir silently leaves files behind when they're locked by another
:: process (file explorer preview, AV scanner, running launcher).
:: That used to produce a "mixed Python" staging — a partial 3.12 leftover
:: + new 3.14 extraction, which then crashed pip bootstrap. Use PowerShell
:: Remove-Item which respects locks better, then verify the dir is gone.
:: If something IS holding files, fail loudly so the user can close it
:: instead of producing a half-broken install.
if exist "%STAGE%" (
    echo  [*] Cleaning previous staging directory...
    powershell -NoProfile -Command "Remove-Item -Path '%STAGE%' -Recurse -Force -ErrorAction SilentlyContinue"
    if exist "%STAGE%" (
        echo  [!] Could not fully remove staging directory.
        echo      Some file is locked by another process. Close the launcher,
        echo      any skill windows, file explorer previews, and AV scanners,
        echo      then re-run this script.
        goto :fail
    )
)
mkdir "%STAGE%"

:: ── Step 2: Download Python embeddable ──
set "PY_ARCHIVE=%BUILD%%PYTHON_ZIP%"
if not exist "%PY_ARCHIVE%" (
    echo  [*] Downloading Python %PYTHON_VER% embeddable...
    curl -L -o "%PY_ARCHIVE%" "%PYTHON_URL%"
    if !errorlevel! neq 0 (
        echo  [!] Failed to download Python. Check your internet connection.
        goto :fail
    )
) else (
    echo  [OK] Python archive already downloaded.
)

:: ── Step 3: Extract Python into staging/python/ ──
echo  [*] Extracting Python embeddable...
mkdir "%STAGE%\python"
powershell -Command "Expand-Archive -Force '%PY_ARCHIVE%' '%STAGE%\python'"

:: ── Step 4: Enable site-packages in the ._pth file ──
echo  [*] Enabling site-packages in Python...
for %%F in ("%STAGE%\python\python*._pth") do (
    echo.>> "%%F"
    echo import site>> "%%F"
)

:: ── Step 5: Bootstrap pip ──
set "GETPIP=%BUILD%get-pip.py"
if not exist "%GETPIP%" (
    echo  [*] Downloading get-pip.py...
    curl -L -o "%GETPIP%" "%GETPIP_URL%"
)
echo  [*] Installing pip...
"%STAGE%\python\python.exe" "%GETPIP%" --no-warn-script-location --quiet
if !errorlevel! neq 0 (
    echo  [!] pip bootstrap failed.
    goto :fail
)

:: ── Step 6: Install runtime dependencies ──
:: IMPORTANT: onnxruntime + numpy + scipy + Pillow + onnx are required
:: by the Mining Signals HUD reader (mass/resistance/instability OCR
:: via the digit CNN) AND the signal scanner (NCC anchor, comma
:: masking, CC labelling, multi-recipe binarize).
::
:: scipy specifically is used by:
::   * ocr/sc_ocr/api.py — _adaptive_binarize_multi (multi-recipe
::                          binarization with span-count selection)
::   * ocr/sc_ocr/signal_anchor.py — connected-component labelling
::                          for icon validation
::   * ocr/sc_ocr/label_match.py   — template matching
::
:: Without it the signal scanner silently falls through to Tesseract-
:: only mode (much slower + less accurate). Past builds shipped
:: without scipy and the failure mode was invisible to end users
:: (CNN voters silently no-op'd via try/except).
echo  [*] Installing PySide6, requests, pynput, mss, pytesseract, Pillow, scipy, onnxruntime, numpy...
:: Each package spec is quoted so cmd doesn't parse the `>=` as a
:: stdout redirect (which created stray zero-byte files like
:: build\1.15.0, build\1.24.0, build\42.0.0 from earlier builds —
:: harmless cosmetic cruft but noise in the build dir).  Fixed in
:: the v2.2.10 audit pass.
"%STAGE%\python\python.exe" -m pip install "PySide6>=6.5.0" "requests>=2.28.0" "pynput>=1.7.6" "mss>=9.0.0" "pytesseract>=0.3.10" "Pillow>=10.0.0" "cryptography>=42.0.0" "onnxruntime>=1.17.0" "numpy>=1.24.0" "scipy>=1.11.0" "onnx>=1.15.0" --no-warn-script-location --quiet
if !errorlevel! neq 0 (
    echo  [!] Dependency installation failed.
    goto :fail
)
echo  [OK] Dependencies installed.

:: pip generates Scripts\*.exe wrappers (pip.exe, pyside6-*, etc.) with
:: the build-machine python path baked into a shebang. SC_Toolbox calls
:: every package as `python -m <pkg>`, so these wrappers are unused at
:: runtime — and shipping them leaks the build-machine username.
if exist "%STAGE%\python\Scripts" rmdir /s /q "%STAGE%\python\Scripts"

:: PySide6 ships C++ build intermediates (objects-Debug / objects-RelWithDebInfo
:: dirs, *.obj, *.prl) and onnx bundles its full test-suite fixtures. None are used
:: at runtime, and their deep paths blow past Windows' 260-char MAX_PATH, which
:: aborts Inno Setup mid-compress ("filename ... syntax is incorrect"). Prune them
:: so the installer both builds and stays lean. (Elah 2026-07-22, after 2.3.0 build)
set "PYSIDE6=%STAGE%\python\Lib\site-packages\PySide6"
if exist "%PYSIDE6%" (
    for /d /r "%PYSIDE6%" %%D in (objects-*) do rmdir /s /q "%%D" 2>nul
    del /s /q "%PYSIDE6%\*.obj" 2>nul
    del /s /q "%PYSIDE6%\*.prl" 2>nul
)
if exist "%STAGE%\python\Lib\site-packages\onnx\backend\test" rmdir /s /q "%STAGE%\python\Lib\site-packages\onnx\backend\test"

:: ── Step 6b: Bundle Tesseract OCR ──
:: Prefer a system install (fastest), fall back to downloading the
:: official installer if not present. Either way, validate the
:: binary + tessdata ended up in staging or fail the build.
set "TESS_SRC=C:\Program Files\Tesseract-OCR"
set "TESS_DEST=%STAGE%\tools\Mining_Signals\tesseract"
set "TESS_INSTALLER=%BUILD%%TESSERACT_INSTALLER%"

if exist "%TESS_SRC%\tesseract.exe" (
    echo  [*] Bundling Tesseract from system install at %TESS_SRC%...
    xcopy "%TESS_SRC%" "%TESS_DEST%\" /s /i /q >nul
) else (
    echo  [*] Tesseract not installed system-wide — downloading installer...
    if not exist "%TESS_INSTALLER%" (
        curl -L -o "%TESS_INSTALLER%" "%TESSERACT_URL%"
        if !errorlevel! neq 0 (
            echo  [!] Failed to download Tesseract installer.
            goto :fail
        )
    )
    :: Silent-install the downloaded installer to a temp location,
    :: then copy the binary + tessdata out of it.
    set "TESS_TMP=%BUILD%_tess_tmp"
    if exist "!TESS_TMP!" rmdir /s /q "!TESS_TMP!"
    echo  [*] Extracting Tesseract...
    "%TESS_INSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /DIR="!TESS_TMP!" /NOCANCEL /NORESTART
    if not exist "!TESS_TMP!\tesseract.exe" (
        echo  [!] Tesseract extraction failed — no tesseract.exe produced.
        goto :fail
    )
    xcopy "!TESS_TMP!" "%TESS_DEST%\" /s /i /q >nul
    rmdir /s /q "!TESS_TMP!"
)

:: Validate Tesseract bundled correctly
if not exist "%TESS_DEST%\tesseract.exe" (
    echo  [!] Tesseract bundling failed: tesseract.exe missing from staging
    goto :fail
)
if not exist "%TESS_DEST%\tessdata\eng.traineddata" (
    echo  [!] Tesseract bundling failed: eng.traineddata missing from staging
    goto :fail
)
echo  [OK] Tesseract bundled and validated.

:: ── Step 7: Stage runtime source files ──
echo.
echo  [*] Staging runtime files...

:: Root-level runtime files
copy "%ROOT%\skill_launcher.py"             "%STAGE%\" >nul
copy "%ROOT%\skill_launcher_settings.json"  "%STAGE%\" >nul
copy "%ROOT%\pyproject.toml"                "%STAGE%\" >nul
copy "%ROOT%\README.txt"                    "%STAGE%\" >nul
copy "%ROOT%\README.md"                     "%STAGE%\" >nul 2>nul

:: Installed-version launcher (uses bundled Python, not system Python)
copy "%BUILD%SC_Toolbox_Installed.vbs"      "%STAGE%\SC_Toolbox.vbs" >nul

:: App icon
copy "%ROOT%\assets\sc_toolbox.ico"         "%STAGE%\sc_toolbox.ico" >nul

:: core/
xcopy "%ROOT%\core\*.py" "%STAGE%\core\" /s /i /q >nul
:: Remove test files from core
if exist "%STAGE%\core\tests" rmdir /s /q "%STAGE%\core\tests"

:: shared/
xcopy "%ROOT%\shared\*.py" "%STAGE%\shared\" /s /i /q >nul
xcopy "%ROOT%\shared\qt\fonts\*.*" "%STAGE%\shared\qt\fonts\" /s /i /q >nul
:: Remove test files from shared
if exist "%STAGE%\shared\tests" rmdir /s /q "%STAGE%\shared\tests"

:: ui/
xcopy "%ROOT%\ui\*.py" "%STAGE%\ui\" /s /i /q >nul

:: skills/ — copy each skill, then prune non-runtime files
echo  [*] Staging skills...
for %%S in (Cargo_loader Craft_Database DPS_Calculator Market_Finder Mining_Loadout Mission_Database Mouse_Blocker Trade_Hub) do (
    if exist "%ROOT%\skills\%%S" (
        xcopy "%ROOT%\skills\%%S" "%STAGE%\skills\%%S\" /s /i /q >nul

        :: Remove cache files
        del /q "%STAGE%\skills\%%S\.cargo_cache.json" 2>nul
        del /q "%STAGE%\skills\%%S\.erkul_cache.json" 2>nul
        del /q "%STAGE%\skills\%%S\.fy_hardpoints_cache.json" 2>nul
        del /q "%STAGE%\skills\%%S\.uex_cache.json" 2>nul
        del /q "%STAGE%\skills\%%S\.scmdb_cache*.json" 2>nul
        if exist "%STAGE%\skills\%%S\.craft_cache" rmdir /s /q "%STAGE%\skills\%%S\.craft_cache"
        if exist "%STAGE%\skills\%%S\.api_cache" rmdir /s /q "%STAGE%\skills\%%S\.api_cache"
        :: Remove log files
        del /q "%STAGE%\skills\%%S\*.log" 2>nul
        del /q "%STAGE%\skills\%%S\*.log.*" 2>nul
        del /q "%STAGE%\skills\%%S\nul.lock" 2>nul
        del /q "%STAGE%\skills\%%S\_debug.log" 2>nul
        :: Remove dev/audit files
        del /q "%STAGE%\skills\%%S\*_audit*.py" 2>nul
        del /q "%STAGE%\skills\%%S\*_audit*.txt" 2>nul
        del /q "%STAGE%\skills\%%S\audit_report.txt" 2>nul
        del /q "%STAGE%\skills\%%S\erkul_power_formulas.js" 2>nul
        del /q "%STAGE%\skills\%%S\ERKUL_PARITY_FIX_PROMPT.md" 2>nul
        del /q "%STAGE%\skills\%%S\INSTALL.md" 2>nul
        del /q "%STAGE%\skills\%%S\validate_calc.py" 2>nul
        del /q "%STAGE%\skills\%%S\generate_layout.py" 2>nul
        del /q "%STAGE%\skills\%%S\cargo_grid_editor.html" 2>nul
        del /q "%STAGE%\skills\%%S\requirements.txt" 2>nul
        :: pytest coverage data — contains absolute source paths.
        del /q "%STAGE%\skills\%%S\.coverage" 2>nul
    )
)

:: tools/ — copy each tool, then prune non-runtime files
echo  [*] Staging tools...
for %%T in (Battle_Buddy Mining_Signals PlayTime_Calculator) do (
    if exist "%ROOT%\tools\%%T" (
        xcopy "%ROOT%\tools\%%T" "%STAGE%\tools\%%T\" /s /i /q >nul
        :: Remove cache, log, and dev files
        del /q "%STAGE%\tools\%%T\.*_cache*.json" 2>nul
        del /q "%STAGE%\tools\%%T\*.log" 2>nul
        del /q "%STAGE%\tools\%%T\*.log.*" 2>nul
        del /q "%STAGE%\tools\%%T\requirements.txt" 2>nul
        :: Remove debug screenshots from scanner output
        del /q "%STAGE%\tools\%%T\debug_*.png" 2>nul
        del /q "%STAGE%\tools\%%T\_debug_*.png" 2>nul
        del /q "%STAGE%\tools\%%T\_*.png" 2>nul
        del /q "%STAGE%\tools\%%T\_sample_*.png" 2>nul
        del /q "%STAGE%\tools\%%T\_test_*.png" 2>nul
        del /q "%STAGE%\tools\%%T\refinery_ocr_*.png" 2>nul
        del /q "%STAGE%\tools\%%T\refinery_ocr_debug.txt" 2>nul
        :: Remove tesseract installer if accidentally staged
        del /q "%STAGE%\tools\%%T\tesseract\tesseract-setup.exe" 2>nul
        :: Per-user dev/runtime artifacts that contain absolute paths
        :: (Claude Code session config, labeler error log, training metadata
        :: JSON sidecar of the OCR model). The .onnx model itself is binary
        :: weights only — safe to ship.
        if exist "%STAGE%\tools\%%T\.claude" rmdir /s /q "%STAGE%\tools\%%T\.claude"
        del /q "%STAGE%\tools\%%T\labeler_err.txt" 2>nul
        del /q "%STAGE%\tools\%%T\labeler.log" 2>nul
        del /q "%STAGE%\tools\%%T\.coverage" 2>nul
        del /q "%STAGE%\tools\%%T\ocr\models\model_signal_cnn.json" 2>nul
    )
)

:: Mining_Signals: remove training_data (large, only needed for
:: offline model retraining — not used at runtime) and any
:: per-user captures that leaked into the dev tree.
if exist "%STAGE%\tools\Mining_Signals\training_data" (
    echo  [*] Removing training_data/ from staging (dev-only, ~50-500 MB)
    rmdir /s /q "%STAGE%\tools\Mining_Signals\training_data"
)
:: Recreate an empty training_data/ so training_collector.py can
:: write to it if the user ever enables harvest.
mkdir "%STAGE%\tools\Mining_Signals\training_data" 2>nul
for %%D in (0 1 2 3 4 5 6 7 8 9) do (
    mkdir "%STAGE%\tools\Mining_Signals\training_data\%%D" 2>nul
)

:: Sanitize Mining_Signals config — the dev config contains personal
:: screen coordinates (hud_region, ocr_region) and personal filesystem
:: paths (ship_loadouts, ledger_file). Replace it with a clean default
:: so new installs start with null regions and prompt the user to set
:: them up via the in-app region selectors.
:: Mirrors the dev-machine config exactly — same keys, same defaults,
:: same scan_interval_seconds=3 (1 was too aggressive; scans piled up
:: behind each other on the bundled Python causing 95%% of scan ticks
:: to be skipped). Personal/per-machine fields (paths, screen regions,
:: active ship) are nulled. Everything else matches local so behavior
:: is identical out-of-the-box.
echo  [*] Sanitizing Mining_Signals config (matching local defaults, stripping personal data)...
(
    echo {
    echo   "refresh_interval_minutes": 60,
    echo   "scan_interval_seconds": 3,
    echo   "ocr_region": null,
    echo   "hud_region": null,
    echo   "ship_loadouts": {
    echo     "golem": null,
    echo     "prospector": null,
    echo     "mole": null
    echo   },
    echo   "active_ship": null,
    echo   "gadget_quantities": {},
    echo   "always_use_best_gadget": false,
    echo   "fleet_loadouts": [],
    echo   "fleet_player_counts": {},
    echo   "module_uses_remaining": {},
    echo   "game_dir": null,
    echo   "refinery_picked_up": [],
    echo   "refinery_deleted": [],
    echo   "refinery_ocr_region": null,
    echo   "refinery_orders": [],
    echo   "refinery_auto_scan": false,
    echo   "calc_mode": "fleet",
    echo   "salvage_loadouts": [],
    echo   "ledger_file": null,
    echo   "bubble_position": null,
    echo   "break_bubble_position": null
    echo }
) > "%STAGE%\tools\Mining_Signals\mining_signals_config.json"

:: Also strip any personal ledger / fleet / loadout data that may
:: have been xcopy'd alongside the source.
del /q "%STAGE%\tools\Mining_Signals\mining_ledger.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\fleet_snapshots.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\refinery_orders.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\mining_signals.log" 2>nul
del /q "%STAGE%\tools\Mining_Signals\mining_signals.log.*" 2>nul

:: Strip OCR training infrastructure — end users do not retrain models.
:: All training collection / labeling tools are dev-only. This shrinks
:: the installer by ~150 MB and removes the screenshot dataset
:: (potential third-party data exposure surface).
echo  [*] Removing OCR training data, scripts, and dev artifacts...
:: Training datasets (the LIVE training_data/ stub from above is preserved).
:: NOTE: training_data_blacklist is intentionally NOT in this list — despite
:: the misleading directory name, signal_anchor.py loads its location-pin
:: icon template from there (`_ICON_TEMPLATES_DIR = _TOOL_DIR / "training_data_blacklist"`).
:: Stripping it makes _load_icon_templates() return [], which makes
:: find_icon() return None on every frame, which keeps sig_present=False
:: forever — i.e. the signature scanner silently no-ops. Keep the dir.
for %%D in (
    training_data_clean
    training_data_crnn
    training_data_panels
    training_data_split
    training_data_user_panel
    training_data_user_panel_inv
    training_data_user_sig
    template_source_panels
    scripts
    debug_glyphs
) do (
    if exist "%STAGE%\tools\Mining_Signals\%%D" rmdir /s /q "%STAGE%\tools\Mining_Signals\%%D"
)
:: Restore runtime files from scripts/ that the wholesale rmdir above
:: removed. These three are imported at runtime — not dev-only:
::   * extract_labeled_glyphs.py — api.py imports its
::     _locate_icon_via_blacklist_match + _isolate_main_row helpers
::     for the signature-scan pipeline.
::   * signature_finder_viewer.py / glyph_reader_viewer.py — UI popouts
::     opened from buttons in the calibration dialog.
:: All other ~40 files in scripts/ are training/labeling tooling and
:: stay stripped.
mkdir "%STAGE%\tools\Mining_Signals\scripts" 2>nul
for %%F in (
    extract_labeled_glyphs.py
    signature_finder_viewer.py
    glyph_reader_viewer.py
) do (
    if exist "%ROOT%\tools\Mining_Signals\scripts\%%F" (
        copy "%ROOT%\tools\Mining_Signals\scripts\%%F" "%STAGE%\tools\Mining_Signals\scripts\%%F" >nul
    )
)
:: Trainer modules and synthesis helpers in ocr/ — no runtime imports.
:: NOTE: templates_furore.py was previously listed here but it IS a
:: runtime import (sc_ocr/api.py imports it for the template-voter
:: fallback when the neural engines disagree). Keep it in staging.
for %%F in (
    pretrain_crnn.py
    train_crnn.py
    train_model.py
    train_sklearn.py
    train_torch.py
    synth_data.py
) do (
    del /q "%STAGE%\tools\Mining_Signals\ocr\%%F" 2>nul
)
:: Debug captures, font-comparison dumps, and dev-tree state files.
del /q "%STAGE%\tools\Mining_Signals\debug_yt_frame.jpg" 2>nul
del /q "%STAGE%\tools\Mining_Signals\debug_yt_frame_right.jpg" 2>nul
del /q "%STAGE%\tools\Mining_Signals\debug_overlay_*.txt" 2>nul
del /q "%STAGE%\tools\Mining_Signals\capture_diag.txt" 2>nul
del /q "%STAGE%\tools\Mining_Signals\labeler_*.txt" 2>nul
del /q "%STAGE%\tools\Mining_Signals\font_compare.png" 2>nul
del /q "%STAGE%\tools\Mining_Signals\furore_compare.png" 2>nul
del /q "%STAGE%\tools\Mining_Signals\.dual_capture_state.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\.gitignore" 2>nul
del /q "%STAGE%\tools\Mining_Signals\run_voice_tester.bat" 2>nul

:: Strip torch.onnx stack-trace metadata from any ONNX model that
:: still embeds it. PyTorch's default ONNX exporter records the
:: full python file path of every layer in pkg.torch.onnx.stack_trace
:: per node — leaks the build-machine username. Helper script
:: preserves external-data layout (.onnx + .onnx.data).
echo  [*] Stripping torch metadata from ONNX models...
"%STAGE%\python\python.exe" "%BUILD%strip_onnx_metadata.py" "%STAGE%\tools\Mining_Signals\ocr\models"
if !errorlevel! neq 0 (
    echo  [!] ONNX metadata strip failed — installer may leak username in model files.
    set "VALIDATION_OK=0"
)

:: Belt-and-suspenders privacy scrub (Elah 2026-07-22). The strip above only covers models\*.onnx and
:: misses dev scripts, json sidecars, csv/out training logs, and backup model dirs — all of which also
:: embedded the build-machine path C:\Users\<user>\... (the 2.3.0 staging leaked the username in ~78
:: files). This prunes backup model dirs and same-length-scrubs the username token out of EVERY file
:: under the project tree, then FAILS the build if any trace remains (fail-closed on privacy). Username
:: comes from %USERNAME% so no name is hardcoded in this committed script.
echo  [*] Scrubbing build-machine username (%USERNAME%) from staged project files...
"%STAGE%\python\python.exe" "%BUILD%sanitize_staging.py" "%STAGE%" --user "%USERNAME%" --apply
if !errorlevel! neq 0 (
    echo  [!] Privacy scrub FAILED — username may still be present in staging. Aborting to avoid a public leak.
    exit /b 1
)

:: ── Step 7b: Deterministic Paddle sidecar setup ──
:: The Paddle sidecar uses its own bundled Python 3.13 with
:: paddlepaddle + paddleocr installed. When xcopy picks it up from
:: the dev tree it "just works", but if the dev tree is clean this
:: silently ships without Paddle and the refinery scanner falls
:: back to Tesseract-only. Instead, verify the sidecar is present
:: and functional; if missing, set it up from scratch.
set "PADDLE_DIR=%STAGE%\tools\Mining_Signals\py313_paddleocr"
set "PADDLE_PY=%PADDLE_DIR%\python.exe"
set "PY313_VER=3.13.1"
set "PY313_ZIP=python-%PY313_VER%-embed-amd64.zip"
set "PY313_URL=https://www.python.org/ftp/python/%PY313_VER%/%PY313_ZIP%"

if exist "%PADDLE_PY%" (
    echo  [OK] Paddle sidecar Python 3.13 already staged.
) else (
    echo  [*] Paddle sidecar missing — setting up from scratch...
    set "PY313_ARCHIVE=%BUILD%%PY313_ZIP%"
    if not exist "!PY313_ARCHIVE!" (
        echo  [*] Downloading Python %PY313_VER% embeddable...
        curl -L -o "!PY313_ARCHIVE!" "%PY313_URL%"
        if !errorlevel! neq 0 (
            echo  [!] Failed to download Python 3.13. Paddle sidecar will not work.
            goto :fail
        )
    )
    mkdir "%PADDLE_DIR%" 2>nul
    echo  [*] Extracting Python 3.13 embeddable into sidecar dir...
    powershell -Command "Expand-Archive -Force '!PY313_ARCHIVE!' '%PADDLE_DIR%'"
    :: Enable site-packages in the ._pth file
    for %%F in ("%PADDLE_DIR%\python*._pth") do (
        echo.>> "%%F"
        echo import site>> "%%F"
    )
    :: Bootstrap pip into the sidecar's Python 3.13
    echo  [*] Bootstrapping pip in Paddle sidecar...
    "%PADDLE_PY%" "%GETPIP%" --no-warn-script-location --quiet
    if !errorlevel! neq 0 (
        echo  [!] pip bootstrap failed in Paddle sidecar.
        goto :fail
    )
    rem Install paddlepaddle 3.0.0 + paddleocr + dependencies.
    rem paddlepaddle 3.3.1 crashes on first inference — pin to 3.0.0.
    rem --only-binary=:all: forces wheel-only resolution. Without it pip will
    rem pick python-bidi==0.6.9 (sdist + Rust/maturin build backend) which
    rem fails on the embeddable Python with no Rust toolchain. Wheel-only
    rem resolution falls back to python-bidi==0.6.7 (cp313 wheel exists).
    echo  [*] Installing paddlepaddle==3.0.0, paddleocr, numpy, Pillow...
    "%PADDLE_PY%" -m pip install --only-binary=:all: paddlepaddle==3.0.0 paddleocr numpy Pillow --no-warn-script-location --quiet
    if !errorlevel! neq 0 (
        echo  [!] Paddle sidecar dependency install failed.
        goto :fail
    )
    echo  [OK] Paddle sidecar installed from scratch.
)

:: Same Scripts\ wrapper cleanup as the main Python — these .exes
:: bake the build-machine path into a shebang and aren't called at
:: runtime (paddleocr is invoked as `python -m paddleocr`).
if exist "%PADDLE_DIR%\Scripts" rmdir /s /q "%PADDLE_DIR%\Scripts"

:: ── Step 7b.1: Prune Paddle sidecar bloat ──
:: paddlepaddle + paddleocr pull in many transitive deps (modelscope,
:: c++ headers, tests, docs) that aren't needed at runtime AND have
:: deeply nested paths that bust the Windows 260-char MAX_PATH limit
:: Inno Setup respects. Prune them to make the installer buildable.
echo  [*] Pruning Paddle sidecar bloat (modelscope, paddle headers, etc.)...
set "PY313_SP=%PADDLE_DIR%\Lib\site-packages"
:: Narrow, safe prune — ONLY files that are guaranteed unused at
:: runtime. Aggressive pruning of subpackages like paddlex/modules,
:: paddle/distributed, or deleting modelscope wholesale breaks
:: paddleocr's import chain (learned the hard way in v2.3.0).
::
:: 1. paddle C++ headers — only needed to compile custom ops,
::    never imported by Python at runtime. ~200 MB of .h files.
if exist "%PY313_SP%\paddle\include" rmdir /s /q "%PY313_SP%\paddle\include"
:: 2. modelscope's custom_datasets subtree — dataset loaders for
::    non-OCR tasks (image quality assessment, video segmentation,
::    etc.). Not imported by PaddleOCR's text recognition pipeline
::    and has the deepest paths (312 chars) that bust MAX_PATH.
if exist "%PY313_SP%\modelscope\msdatasets\dataset_cls\custom_datasets" rmdir /s /q "%PY313_SP%\modelscope\msdatasets\dataset_cls\custom_datasets"
:: 2b. modelscope's CV-task model implementations (face_detection,
::     animal_recognition, abnormal_object_detection, etc.) — none
::     used by PaddleOCR text recognition. ~50 subtrees, many of which
::     have 270+ char paths (mmdet_ms/roi_head/roi_extractors/...).
::     Keep the parent dir + __init__.py so `import modelscope.models.cv`
::     still works.
if exist "%PY313_SP%\modelscope\models\cv" (
    for /d %%D in ("%PY313_SP%\modelscope\models\cv\*") do rmdir /s /q "%%D"
)
:: 2c. modelscope diffusion / audio pipelines — not used by OCR. These
::     each contain a single file or directory with paths past 240 chars.
if exist "%PY313_SP%\modelscope\pipelines\multi_modal\diffusers_wrapped" rmdir /s /q "%PY313_SP%\modelscope\pipelines\multi_modal\diffusers_wrapped"
if exist "%PY313_SP%\modelscope\pipelines\multi_modal\disco_guided_diffusion_pipeline" rmdir /s /q "%PY313_SP%\modelscope\pipelines\multi_modal\disco_guided_diffusion_pipeline"
if exist "%PY313_SP%\modelscope\trainers\multi_modal\efficient_diffusion_tuning" rmdir /s /q "%PY313_SP%\modelscope\trainers\multi_modal\efficient_diffusion_tuning"
del /q "%PY313_SP%\modelscope\pipelines\audio\speaker_diarization_semantic_speaker_turn_detection_pipeline.py" 2>nul
:: 2d. paddlex non-OCR config / inference subtrees — image classification,
::     object detection, instance segmentation, vehicle/pedestrian attributes,
::     and the doc_vlm / open_vocabulary_detection inference models.
::     PaddleOCR's text-recognition pipeline does not import these.
for %%D in (
    image_classification
    image_multilabel_classification
    instance_segmentation
    object_detection
    pedestrian_attribute_recognition
    vehicle_attribute_recognition
    multilabel_classification
) do (
    if exist "%PY313_SP%\paddlex\configs\modules\%%D" rmdir /s /q "%PY313_SP%\paddlex\configs\modules\%%D"
)
:: NOTE (v2.2.7+): we no longer delete doc_vlm or open_vocabulary_detection
:: from paddlex/inference/models/. Both are unused at runtime, BUT paddlex's
:: own __init__.py chains UNCONDITIONALLY import them via:
::     paddlex/inference/__init__.py
::       → paddlex/inference/pipelines/__init__.py
::         → from .doc_understanding import DocUnderstandingPipeline
::           → paddlex/inference/pipelines/doc_understanding/pipeline.py
::             → from ...models.doc_vlm.result import DocVLMResult  ← FAILS
::
:: Deleting these dirs makes the daemon fail to start with:
::   "daemon fatal: No module named 'paddlex.inference.models.doc_vlm'"
:: which kills the PaddleOCR voter and degrades signal OCR throughput.
:: Cost of keeping them: ~30 MB. Worth it.
:: for %%D in (doc_vlm open_vocabulary_detection) do (
::     if exist "%PY313_SP%\paddlex\inference\models\%%D" rmdir /s /q "%PY313_SP%\paddlex\inference\models\%%D"
:: )
:: 2e. paddle GPU compile-config tile data — only used if running on
::     specific NVIDIA GPUs (V100, A100). The OCR sidecar runs CPU-only
::     in the shipped installer, so this is dead weight with deep paths.
if exist "%PY313_SP%\paddle\cinn_config\tile_config" (
    for /d %%D in ("%PY313_SP%\paddle\cinn_config\tile_config\NVGPU_*") do rmdir /s /q "%%D"
)
:: 3. tests / examples / docs subtrees — never imported.
for %%P in (paddle paddleocr paddlex numpy pandas) do (
    if exist "%PY313_SP%\%%P\tests" rmdir /s /q "%PY313_SP%\%%P\tests"
    if exist "%PY313_SP%\%%P\test" rmdir /s /q "%PY313_SP%\%%P\test"
    if exist "%PY313_SP%\%%P\examples" rmdir /s /q "%PY313_SP%\%%P\examples"
    if exist "%PY313_SP%\%%P\docs" rmdir /s /q "%PY313_SP%\%%P\docs"
)
:: 4. __pycache__ dirs — regenerated on first import, saves ~100 MB
::    and these are the files with the very longest paths.
powershell -Command "Get-ChildItem -Path '%PADDLE_DIR%' -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq '__pycache__' } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"

:: ── Step 7c: Validate critical runtime components ──
echo.
echo  [*] Validating staging integrity...
set "VALIDATION_OK=1"

:: Mining_Signals ONNX models — validate the full voter ensemble.
::
:: Pre-v2.2.10 the build only checked model_cnn.onnx (HUD digit CNN).
:: A user crash log surfaced the gap: signal_anchor reported BOTH
:: rgb_cnn=unavailable AND gray_cnn=unavailable, meaning the signal-
:: side CNN models hadn't loaded. With every voter abstaining the
:: anchor rejected the signature panel and the scanner silently
:: returned nothing. Any missing model file produces the same class
:: of failure — the corresponding CNN/CRNN voter sticks in the
:: "unavailable" state for the process lifetime and the runtime
:: falls back to weaker readers (or no read at all) without telling
:: the user.
::
:: Each ".onnx" pairs with an ".onnx.data" external-weights file
:: (CNN weights exceed the 2GB single-protobuf limit).
::
::   HUD digit OCR (mass / resistance / instability):
::     model_cnn          — gray primary
::     model_cnn_inv      — gray secondary (inverted polarity)
::     model_crnn         — whole-value sequence reader
::
::   Signal-panel OCR (signature scanner):
::     model_signal_cnn          — gray voter (anchor + per-glyph)
::     model_signal_inv_cnn      — inverted secondary
::     model_signal_rgb_cnn_v2   — RGB voter + v2.2.9 PRIMARY reader
::     model_signal_rgb_inv_cnn  — RGB inverted (primary's polarity pair)
::     model_signal_crnn_rgb     — RGB CRNN, v2.2.9 SECONDARY reader
::
::   Template voter (sixth voter, deterministic furore-font match):
::     furore_templates.npz
for %%M in (
    model_cnn.onnx
    model_cnn.onnx.data
    model_cnn_inv.onnx
    model_cnn_inv.onnx.data
    model_crnn.onnx
    model_crnn.onnx.data
    model_signal_cnn.onnx
    model_signal_cnn.onnx.data
    model_signal_inv_cnn.onnx
    model_signal_inv_cnn.onnx.data
    model_signal_rgb_cnn_v2.onnx
    model_signal_rgb_cnn_v2.onnx.data
    model_signal_rgb_inv_cnn.onnx
    model_signal_rgb_inv_cnn.onnx.data
    model_signal_crnn_rgb.onnx
    model_signal_crnn_rgb.onnx.data
    furore_templates.npz
) do (
    if not exist "%STAGE%\tools\Mining_Signals\ocr\models\%%M" (
        echo  [!] MISSING: ocr\models\%%M
        set "VALIDATION_OK=0"
    )
)

:: Tesseract binary (all OCR paths depend on this)
if not exist "%STAGE%\tools\Mining_Signals\tesseract\tesseract.exe" (
    echo  [!] MISSING: tesseract\tesseract.exe — all OCR broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\tools\Mining_Signals\tesseract\tessdata\eng.traineddata" (
    echo  [!] MISSING: tesseract\tessdata\eng.traineddata — Tesseract broken
    set "VALIDATION_OK=0"
)

:: Paddle sidecar (refinery + light-bg HUD scanning)
if not exist "%PADDLE_PY%" (
    echo  [!] MISSING: py313_paddleocr\python.exe — Paddle OCR broken
    set "VALIDATION_OK=0"
)

:: Runtime helpers from scripts/ — api.py + calibration_dialog import these.
:: The wholesale rmdir of scripts/ above must be balanced by the copy-back
:: step that restores the three runtime files. If any of them go missing
:: the signal CNN voter silently no-ops and two UI buttons error out.
if not exist "%STAGE%\tools\Mining_Signals\scripts\extract_labeled_glyphs.py" (
    echo  [!] MISSING: scripts\extract_labeled_glyphs.py — signal CNN voter broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\tools\Mining_Signals\scripts\signature_finder_viewer.py" (
    echo  [!] MISSING: scripts\signature_finder_viewer.py — calibration "Signature Finder" button broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\tools\Mining_Signals\scripts\glyph_reader_viewer.py" (
    echo  [!] MISSING: scripts\glyph_reader_viewer.py — calibration "Glyph Reader" button broken
    set "VALIDATION_OK=0"
)
:: Furore-template voter — 6th deterministic voter in the OCR ensemble.
:: api.py imports it as the template-fallback when neural engines disagree.
if not exist "%STAGE%\tools\Mining_Signals\ocr\templates_furore.py" (
    echo  [!] MISSING: ocr\templates_furore.py — template voter broken
    set "VALIDATION_OK=0"
)

:: Main Python deps that the HUD scanner needs at import time
if not exist "%STAGE%\python\Lib\site-packages\onnxruntime" (
    echo  [!] MISSING: onnxruntime pip package — HUD scanner broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\python\Lib\site-packages\numpy" (
    echo  [!] MISSING: numpy pip package — HUD + refinery scanners broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\python\Lib\site-packages\onnx" (
    echo  [!] MISSING: onnx pip package — online_learner ONNX export broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\python\Lib\site-packages\PIL" (
    echo  [!] MISSING: Pillow pip package — all image processing broken
    set "VALIDATION_OK=0"
)
:: scipy is required by sc_ocr's multi-recipe adaptive binarizer,
:: signal_anchor's connected-component labelling, and label_match's
:: template matching. Without it the signal scanner silently falls
:: through to Tesseract-only — a recurring silent-degradation already
:: flagged in this script's pip-install comment above.
if not exist "%STAGE%\python\Lib\site-packages\scipy" (
    echo  [!] MISSING: scipy pip package — signal scanner degrades to Tesseract-only
    set "VALIDATION_OK=0"
)

:: Clean config (no personal data) — fail the build if any home-directory
:: path leaked into the staged config. Catches "C:\Users\<name>\..." and
:: "C:/Users/<name>/..." regardless of who built the installer.
findstr /I /C:"Users\\" /C:"Users/" "%STAGE%\tools\Mining_Signals\mining_signals_config.json" >nul 2>&1
if !errorlevel!==0 (
    echo  [!] POLLUTED: mining_signals_config.json contains a home-directory path
    set "VALIDATION_OK=0"
)

:: Smoke test: catch the config-null-default bug class. The shipped
:: config has keys like `"ocr_region": null` and `"ledger_file":
:: null`. `dict.get(key, default)` returns None (not the default)
:: when the key is present. Downstream code like `region.get("x")`
:: then crashes with AttributeError. Symptoms: bubbles disappear,
:: app won't launch, OCR returns no results.
:: The safe pattern is `config.get(...) or default`.
echo  [*] Smoke-testing config null-handling...
findstr /R /S ^
    /C:"config\.get(\"ocr_region\"," ^
    /C:"config\.get('ocr_region'," ^
    /C:"config\.get(\"hud_region\"," ^
    /C:"config\.get('hud_region'," ^
    /C:"config\.get(\"bubble_position\"," ^
    /C:"config\.get('bubble_position'," ^
    /C:"config\.get(\"break_bubble_position\"," ^
    /C:"config\.get('break_bubble_position'," ^
    /C:"config\.get(\"refinery_ocr_region\"," ^
    /C:"config\.get('refinery_ocr_region'," ^
    /C:"config\.get(\"ledger_file\"," ^
    /C:"config\.get('ledger_file'," ^
    /C:"config\.get(\"game_dir\"," ^
    /C:"config\.get('game_dir'," ^
    /C:"config\.get(\"active_ship\"," ^
    /C:"config\.get('active_ship'," ^
    "%STAGE%\tools\Mining_Signals\*.py" ^
    "%STAGE%\tools\Mining_Signals\ui\*.py" ^
    "%STAGE%\tools\Mining_Signals\services\*.py" ^
    "%STAGE%\tools\Mining_Signals\ocr\*.py" 2>nul
if !errorlevel!==0 (
    echo  [!] SMOKE TEST FAILED — found `config.get("<nullable_key>", default^)` pattern.
    echo      Use `config.get(...^) or default` instead so null values fall back.
    set "VALIDATION_OK=0"
) else (
    echo  [OK] No buggy config.get-with-default patterns found.
)

:: ── Step 7e: Staging import smoke test ──
:: Spawns the staging Python on every skill/tool entry script and
:: verifies it imports cleanly. The file-existence checks above can't
:: see runtime gaps like "missing pip package", "module stripped wrongly",
:: or "transitive import broke" — this catches those before Inno Setup
:: runs. Each skill is tested in its own subprocess so state cannot leak.
::
:: Self-discovers any new skill that ships a skill.json — no edits to
:: this script needed when a new tool is added.
echo  [*] Running staging import smoke test...
"%STAGE%\python\python.exe" "%BUILD%staging_import_test.py" "%STAGE%"
if !errorlevel! neq 0 (
    echo  [!] Staging import smoke test FAILED — see tracebacks above.
    set "VALIDATION_OK=0"
)

if "!VALIDATION_OK!"=="0" (
    echo.
    echo  [!] Staging validation FAILED — see errors above.
    echo      Refusing to build installer with missing components.
    goto :fail
)
echo  [OK] All runtime components validated.

:: Global cleanup — remove all __pycache__, .pytest_cache, tests/ and
:: .claude/ dirs.
::
:: .claude/ holds Claude Code session + git-worktree state that has no
:: business in a shipped installer: it bloats the package, and because
:: a copied worktree directory can later be grabbed as a process CWD,
:: the NEXT build's staging-clean step (Step 1) fails with "being used
:: by another process" — exactly the failure hit during the v2.2.10
:: build. The per-tool strip in Step 7 only covered tools\<T>\.claude;
:: this recursive pass also catches skills\<S>\.claude, the staging
:: root, and any other nesting.
echo  [*] Cleaning staging directory...
powershell -Command "Get-ChildItem -Path '%STAGE%' -Recurse -Directory -Force | Where-Object { $_.Name -in @('__pycache__','.pytest_cache','tests','.claude') } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"

:: locales/ — only include compiled .mo translation files, not the .pot template
:: Copies the full locales/ tree but skips .pot files (dev-only)
if exist "%ROOT%\locales" (
    for /r "%ROOT%\locales" %%F in (*.mo) do (
        set "REL=%%~dpF"
        set "REL=!REL:%ROOT%\locales\=!"
        mkdir "%STAGE%\locales\!REL!" 2>nul
        copy "%%F" "%STAGE%\locales\!REL!" >nul
    )
)

echo  [OK] Staging complete.

:: ── Step 8: Build installer ──
:: Diverges here based on BUILD_MODE:
::   * inno     — runs Inno Setup compiler against SC_Toolbox_Installer.iss,
::                produces Output\SC_Toolbox_Setup_<ver>.exe (single .exe, ~838 MB).
::   * velopack — builds the launcher .exe (replaces the .vbs as the entry
::                point), then runs vpk pack which produces Releases\
::                Setup.exe + nupkg + portable .zip + RELEASES manifest.
::                Delta updates flow through the .nupkg files.
if /I "%BUILD_MODE%"=="velopack" goto :build_velopack
goto :build_inno

:build_inno
echo.
echo  [*] Running Inno Setup compiler...
"%ISCC%" "%BUILD%SC_Toolbox_Installer.iss"
if !errorlevel! neq 0 (
    echo  [!] Inno Setup compilation failed.
    goto :fail
)
echo.
echo  =============================================
echo   [OK] Inno installer built successfully!
echo   Output: %BUILD%Output\SC_Toolbox_Setup.exe
echo  =============================================
echo.
goto :done

:build_velopack
:: ── Step 8a: Build the launcher .exe ──
:: Velopack requires a real .exe entry point that calls
:: VelopackApp.Build().Run() at startup. The launcher project lives in
:: build\launcher\ and is a small WinExe that subprocesses the bundled
:: Python on skill_launcher.py — same behavior as the legacy .vbs but
:: Velopack-aware (handles --squirrel-firstrun, etc.).
echo.
echo  [*] Building launcher .exe (dotnet publish)...
pushd "%BUILD%launcher"
dotnet publish -c Release --nologo -v quiet
if !errorlevel! neq 0 (
    popd
    echo  [!] Launcher build failed.
    goto :fail
)
popd
set "LAUNCHER_EXE=%BUILD%launcher\bin\Release\net8.0-windows\win-x64\publish\SC_Toolbox.exe"
if not exist "%LAUNCHER_EXE%" (
    echo  [!] Launcher .exe missing at expected path: %LAUNCHER_EXE%
    goto :fail
)
copy /Y "%LAUNCHER_EXE%" "%STAGE%\SC_Toolbox.exe" >nul
echo  [OK] Launcher staged.

:: ── Step 8b: Read version from pyproject.toml ──
:: Single source of truth — no more drift between .iss MyAppVersion and
:: pyproject.toml. We pipe the Python reader's output to a temp file
:: then read it via `set /p`. The for /f-with-backticks approach kept
:: tripping cmd's quote/path parser ("filename, directory name, or
:: volume label syntax is incorrect") on quoted paths with spaces.
:: Temp-file pipeline is dumb but bulletproof.
set "PACK_VER="
set "VERSION_FILE=%TEMP%\sc_toolbox_pack_version.txt"
"%STAGE%\python\python.exe" "%BUILD%read_version.py" "%ROOT%\pyproject.toml" > "%VERSION_FILE%"
if !errorlevel! neq 0 (
    echo  [!] Version reader failed.
    goto :fail
)
set /p PACK_VER=<"%VERSION_FILE%"
del /q "%VERSION_FILE%" 2>nul
if not defined PACK_VER (
    echo  [!] Could not read version from pyproject.toml
    goto :fail
)
echo  [*] Packing Velopack release v!PACK_VER!...

:: ── Step 8c: Run vpk pack ──
:: DOTNET_ROLL_FORWARD=Major lets vpk run against ASP.NET Core 10
:: (we don't bundle 9 separately to save install space).
set "DOTNET_ROLL_FORWARD=Major"
vpk pack ^
    -u "SC_Toolbox" ^
    -v "!PACK_VER!" ^
    -p "%STAGE%" ^
    -e "SC_Toolbox.exe" ^
    -i "%ROOT%\assets\sc_toolbox.ico" ^
    --packTitle "SC Toolbox" ^
    --packAuthors "ScPlaceholder" ^
    -o "%BUILD%Releases"
if !errorlevel! neq 0 (
    echo  [!] vpk pack failed.
    goto :fail
)

echo.
echo  =============================================
echo   [OK] Velopack release built successfully!
echo   Output: %BUILD%Releases\
echo     - SC_Toolbox-win-Setup.exe    (full installer)
echo     - SC_Toolbox-!PACK_VER!-full.nupkg
echo     - SC_Toolbox-win-Portable.zip
echo     - RELEASES manifest
echo  =============================================
echo.
goto :done

:fail
echo.
echo  [!] Build failed. See errors above.
exit /b 1

:done
exit /b 0
