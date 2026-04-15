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
set "PYTHON_VER=3.12.9"
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
if exist "%STAGE%" (
    echo  [*] Cleaning previous staging directory...
    rmdir /s /q "%STAGE%"
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
:: IMPORTANT: onnxruntime + numpy are required by the Mining Signals
:: HUD reader (mass/resistance/instability OCR via the digit CNN).
:: Without them the HUD scan silently no-ops while the signal scanner
:: still works — users see "signal scan works, mass/resistance never
:: appears" and assume the tool is broken.
echo  [*] Installing PySide6, requests, pynput, mss, pytesseract, Pillow, onnxruntime, numpy...
"%STAGE%\python\python.exe" -m pip install PySide6>=6.5.0 requests>=2.28.0 pynput>=1.7.6 mss>=9.0.0 pytesseract>=0.3.10 Pillow>=10.0.0 cryptography>=42.0.0 onnxruntime>=1.17.0 numpy>=1.24.0 --no-warn-script-location --quiet
if !errorlevel! neq 0 (
    echo  [!] Dependency installation failed.
    goto :fail
)
echo  [OK] Dependencies installed.

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
for %%S in (Cargo_loader Craft_Database DPS_Calculator Market_Finder Mining_Loadout Mission_Database Trade_Hub) do (
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
    )
)

:: tools/ — copy each tool, then prune non-runtime files
echo  [*] Staging tools...
for %%T in (Battle_Buddy Mining_Signals) do (
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
echo  [*] Sanitizing Mining_Signals config (stripping personal data)...
(
    echo {
    echo   "refresh_interval_minutes": 60,
    echo   "scan_interval_seconds": 1,
    echo   "ocr_region": null,
    echo   "hud_region": null,
    echo   "refinery_ocr_region": null,
    echo   "ship_loadouts": {},
    echo   "active_ship": null,
    echo   "gadget_quantities": {
    echo     "Okunis": 10,
    echo     "BoreMax": 10,
    echo     "OptiMax": 10,
    echo     "Sabir": 10,
    echo     "Stalwart": 10,
    echo     "Waveshift": 10
    echo   },
    echo   "always_use_best_gadget": false,
    echo   "calc_mode": "fleet",
    echo   "bubble_position": null,
    echo   "break_bubble_position": null,
    echo   "ledger_file": null,
    echo   "game_dir": null
    echo }
) > "%STAGE%\tools\Mining_Signals\mining_signals_config.json"

:: Also strip any personal ledger / fleet / loadout data that may
:: have been xcopy'd alongside the source.
del /q "%STAGE%\tools\Mining_Signals\mining_ledger.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\fleet_snapshots.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\refinery_orders.json" 2>nul
del /q "%STAGE%\tools\Mining_Signals\mining_signals.log" 2>nul
del /q "%STAGE%\tools\Mining_Signals\mining_signals.log.*" 2>nul

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
    :: Install paddlepaddle 3.0.0 + paddleocr + dependencies.
    :: paddlepaddle 3.3.1 crashes on first inference — pin to 3.0.0.
    echo  [*] Installing paddlepaddle==3.0.0, paddleocr, numpy, Pillow...
    "%PADDLE_PY%" -m pip install paddlepaddle==3.0.0 paddleocr numpy Pillow --no-warn-script-location --quiet
    if !errorlevel! neq 0 (
        echo  [!] Paddle sidecar dependency install failed.
        goto :fail
    )
    echo  [OK] Paddle sidecar installed from scratch.
)

:: ── Step 7c: Validate critical runtime components ──
echo.
echo  [*] Validating staging integrity...
set "VALIDATION_OK=1"

:: Mining_Signals ONNX model (digit CNN for HUD mass/resistance)
if not exist "%STAGE%\tools\Mining_Signals\ocr\models\model_cnn.onnx" (
    echo  [!] MISSING: ocr\models\model_cnn.onnx — HUD scanner broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\tools\Mining_Signals\ocr\models\model_cnn.onnx.data" (
    echo  [!] MISSING: ocr\models\model_cnn.onnx.data — HUD scanner broken
    set "VALIDATION_OK=0"
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

:: Main Python deps that the HUD scanner needs at import time
if not exist "%STAGE%\python\Lib\site-packages\onnxruntime" (
    echo  [!] MISSING: onnxruntime pip package — HUD scanner broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\python\Lib\site-packages\numpy" (
    echo  [!] MISSING: numpy pip package — HUD + refinery scanners broken
    set "VALIDATION_OK=0"
)
if not exist "%STAGE%\python\Lib\site-packages\PIL" (
    echo  [!] MISSING: Pillow pip package — all image processing broken
    set "VALIDATION_OK=0"
)

:: Clean config (no personal data)
findstr /C:"prjgn" "%STAGE%\tools\Mining_Signals\mining_signals_config.json" >nul 2>&1
if !errorlevel!==0 (
    echo  [!] POLLUTED: mining_signals_config.json contains personal paths
    set "VALIDATION_OK=0"
)

if "!VALIDATION_OK!"=="0" (
    echo.
    echo  [!] Staging validation FAILED — see errors above.
    echo      Refusing to build installer with missing components.
    goto :fail
)
echo  [OK] All runtime components validated.

:: Global cleanup — remove all __pycache__, .pytest_cache, and tests/ dirs
echo  [*] Cleaning staging directory...
powershell -Command "Get-ChildItem -Path '%STAGE%' -Recurse -Directory -Force | Where-Object { $_.Name -in @('__pycache__','.pytest_cache','tests') } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"

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
echo.
echo  [*] Running Inno Setup compiler...
"%ISCC%" "%BUILD%SC_Toolbox_Installer.iss"
if !errorlevel! neq 0 (
    echo  [!] Inno Setup compilation failed.
    goto :fail
)

echo.
echo  =============================================
echo   [OK] Installer built successfully!
echo   Output: %BUILD%Output\SC_Toolbox_Setup.exe
echo  =============================================
echo.
goto :done

:fail
echo.
echo  [!] Build failed. See errors above.
exit /b 1

:done
exit /b 0
