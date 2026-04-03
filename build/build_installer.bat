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
echo  [*] Installing PySide6, requests, pynput, mss, pytesseract...
"%STAGE%\python\python.exe" -m pip install PySide6>=6.5.0 requests>=2.28.0 pynput>=1.7.6 mss>=9.0.0 pytesseract>=0.3.10 cryptography>=42.0.0 --no-warn-script-location --quiet
if !errorlevel! neq 0 (
    echo  [!] Dependency installation failed.
    goto :fail
)
echo  [OK] Dependencies installed.

:: ── Step 6b: Download and bundle Tesseract OCR ──
set "TESS_ARCHIVE=%BUILD%%TESSERACT_INSTALLER%"
if not exist "%TESS_ARCHIVE%" (
    echo  [*] Downloading Tesseract OCR...
    curl -L -o "%TESS_ARCHIVE%" "%TESSERACT_URL%"
    if !errorlevel! neq 0 (
        echo  [!] Tesseract download failed — OCR will auto-download at runtime.
        goto :skip_tesseract
    )
) else (
    echo  [OK] Tesseract installer already downloaded.
)
echo  [*] Extracting Tesseract OCR (silent install)...
"%TESS_ARCHIVE%" /S /D=%STAGE%\tools\Mining_Signals\tesseract
if !errorlevel! neq 0 (
    echo  [!] Tesseract extraction failed — OCR will auto-download at runtime.
) else (
    echo  [OK] Tesseract bundled.
)
:skip_tesseract

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
        :: Remove cache and log files
        del /q "%STAGE%\tools\%%T\.*_cache*.json" 2>nul
        del /q "%STAGE%\tools\%%T\*.log" 2>nul
        del /q "%STAGE%\tools\%%T\*.log.*" 2>nul
        del /q "%STAGE%\tools\%%T\requirements.txt" 2>nul
        :: Remove tesseract installer if accidentally staged
        del /q "%STAGE%\tools\%%T\tesseract\tesseract_setup.exe" 2>nul
    )
)

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
echo.

:done
pause
exit /b
