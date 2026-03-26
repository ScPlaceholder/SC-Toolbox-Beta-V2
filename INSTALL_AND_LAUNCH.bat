@echo off
setlocal enabledelayedexpansion
title SC_Toolbox Installer
color 0B

echo.
echo  =============================================
echo   SC_Toolbox Beta V1 - Installer ^& Launcher
echo  =============================================
echo.

:: ── Check if Python is already installed ──
set "PYTHON_EXE="

:: Check standard Python.org installs
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :found_python
    )
)

:: Check AppData\Local\Python (winget / package manager installs)
:: Search two levels deep to catch paths like pythoncore-3.14-64\python.exe
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%~D\python.exe" (
            set "PYTHON_EXE=%%~D\python.exe"
            goto :found_python
        )
        for /d %%E in ("%%~D\*") do (
            if exist "%%~E\python.exe" (
                set "PYTHON_EXE=%%~E\python.exe"
                goto :found_python
            )
        )
    )
)

:: Check PATH (skip Windows Store stub)
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            set "PYTHON_EXE=%%P"
            goto :found_python
        )
    )
)

:: Check Program Files (both standard and direct)
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%ProgramFiles%\Python\Python%%V\python.exe" (
        set "PYTHON_EXE=%ProgramFiles%\Python\Python%%V\python.exe"
        goto :found_python
    )
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        set "PYTHON_EXE=%ProgramFiles%\Python%%V\python.exe"
        goto :found_python
    )
)

:: Check C:\PythonXX (legacy)
for %%V in (314 313 312 311 310 39 38) do (
    if exist "C:\Python%%V\python.exe" (
        set "PYTHON_EXE=C:\Python%%V\python.exe"
        goto :found_python
    )
)

:: Last resort: use Windows Store python if it actually works with tkinter
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        "%%P" -c "import tkinter" >nul 2>&1
        if !errorlevel!==0 (
            set "PYTHON_EXE=%%P"
            goto :found_python
        )
    )
)

:: Try Python Launcher (py.exe) — installed by default with python.org installs
where py >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if exist "%%P" (
            set "PYTHON_EXE=%%P"
            goto :found_python
        )
    )
)

:: ── Python not found — go install ──
echo  [!] Python is not installed on this system.
goto :install_python

:: ──────────────────────────────────────────────────────
:found_python
:: ──────────────────────────────────────────────────────
echo  [OK] Python found: %PYTHON_EXE%

:: Verify tkinter
"%PYTHON_EXE%" -c "import tkinter" >nul 2>&1
if %errorlevel%==0 goto :tkinter_ok

echo  [!] Python found but tkinter is missing.
echo.
echo  Your Python doesn't include tkinter (needed for the GUI).
echo  This is common with Windows Store or minimal installs.
echo.
goto :install_python

:: ──────────────────────────────────────────────────────
:install_python
:: ──────────────────────────────────────────────────────
echo.
echo  SC_Toolbox requires Python 3.12 with tkinter.
echo  This installer will download it from python.org
echo  and install it automatically (about 30 MB).
echo.
choice /c YN /m "  Install Python 3.12 now?"
if !errorlevel!==2 (
    echo.
    echo  Installation cancelled.
    echo  Please install Python 3.12+ manually from:
    echo    https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: During install, check these options:
    echo    [x] Add Python to PATH
    echo    [x] tcl/tk and IDLE ^(for tkinter^)
    echo.
    pause
    exit /b 1
)

echo.
echo  [*] Downloading Python 3.12.8 installer (about 30 MB)...
echo.

set "INSTALLER=%TEMP%\python-3.12.8-amd64.exe"
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

:: Delete any previous partial download
if exist "%INSTALLER%" del /f "%INSTALLER%" >nul 2>&1

:: Use PowerShell (most reliable on Windows 10/11 — avoids curl alias issues)
echo  Downloading with PowerShell...
powershell -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
    "$ProgressPreference = 'Continue'; " ^
    "try { " ^
    "    Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%INSTALLER%' -UseBasicParsing; " ^
    "    if (Test-Path '%INSTALLER%') { " ^
    "        $sz = (Get-Item '%INSTALLER%').Length; " ^
    "        if ($sz -lt 1000000) { " ^
    "            Write-Host '  [!] Downloaded file too small - may be corrupted'; " ^
    "            Remove-Item '%INSTALLER%' -Force; " ^
    "            exit 1 " ^
    "        } " ^
    "        Write-Host ('  [OK] Download complete ({0:N1} MB)' -f ($sz/1MB)) " ^
    "    } else { " ^
    "        Write-Host '  [!] Download failed - file not created'; " ^
    "        exit 1 " ^
    "    } " ^
    "} catch { " ^
    "    Write-Host ('  [!] Download error: ' + $_.Exception.Message); " ^
    "    exit 1 " ^
    "}"

if not exist "%INSTALLER%" (
    echo.
    echo  [*] PowerShell download failed. Trying curl...
    curl.exe -L -o "%INSTALLER%" "%PYTHON_URL%" --progress-bar 2>&1
)

if not exist "%INSTALLER%" (
    echo.
    echo  [*] curl failed. Trying certutil...
    certutil -urlcache -split -f "%PYTHON_URL%" "%INSTALLER%" 2>&1
)

if not exist "%INSTALLER%" (
    echo.
    echo  [!] All download methods failed.
    echo.
    echo  This usually means your network or antivirus blocked the download.
    echo  Please download Python manually:
    echo.
    echo    https://www.python.org/downloads/release/python-3128/
    echo.
    echo  Download "Windows installer (64-bit)" and run it.
    echo  Make sure to check "Add Python to PATH" and "tcl/tk".
    echo.
    pause
    exit /b 1
)

echo.
echo  [*] Installing Python 3.12...
echo      - For current user only
echo      - Adding to PATH
echo      - Including tkinter (tcl/tk) and pip
echo.

:: Try silent install first (works without admin for per-user install)
echo  [*] Attempting silent install...
"%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1 2>&1
set "INSTALL_ERR=%errorlevel%"

if %INSTALL_ERR% neq 0 (
    echo  [*] Silent install returned code %INSTALL_ERR%.
    echo  [*] Launching interactive installer...
    echo  Please follow the installer prompts. Make sure to:
    echo    - Check "Add python.exe to PATH"
    echo    - Click "Install Now" (or Customize and ensure tcl/tk is checked)
    echo.
    echo  Waiting for installer to finish...
    start /wait "" "%INSTALLER%" PrependPath=1 Include_tcltk=1 Include_pip=1
)

:: Wait a moment for filesystem to settle
timeout /t 3 /nobreak >nul

:: Clean up installer
del /f "%INSTALLER%" >nul 2>&1

:: Find the newly installed Python
set "PYTHON_EXE="
for %%V in (312 314 313 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :verify_new_install
    )
)

:: Check winget path too (two levels deep)
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%~D\python.exe" (
            set "PYTHON_EXE=%%~D\python.exe"
            goto :verify_new_install
        )
        for /d %%E in ("%%~D\*") do (
            if exist "%%~E\python.exe" (
                set "PYTHON_EXE=%%~E\python.exe"
                goto :verify_new_install
            )
        )
    )
)

:: Check Program Files
for %%V in (312 314 313 311 310) do (
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        set "PYTHON_EXE=%ProgramFiles%\Python%%V\python.exe"
        goto :verify_new_install
    )
)

:: Refresh PATH and try where
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            set "PYTHON_EXE=%%P"
            goto :verify_new_install
        )
    )
)

:: Try py launcher
where py >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if exist "%%P" (
            set "PYTHON_EXE=%%P"
            goto :verify_new_install
        )
    )
)

:verify_new_install
if "%PYTHON_EXE%"=="" (
    echo.
    echo  [!] Could not find Python after installation.
    echo.
    echo  Try these steps:
    echo  1. Close this window
    echo  2. Restart your computer (needed to refresh PATH)
    echo  3. Run this installer again
    echo.
    echo  If that doesn't work, install Python manually:
    echo    https://www.python.org/downloads/
    echo  Make sure "Add Python to PATH" is checked.
    echo.
    pause
    exit /b 1
)

:: Verify tkinter on new install
"%PYTHON_EXE%" -c "import tkinter" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [!] Python installed but tkinter is STILL missing.
    echo.
    echo  Please uninstall Python from Settings ^> Apps, then:
    echo  1. Go to https://www.python.org/downloads/
    echo  2. Download Python 3.12
    echo  3. Run the installer
    echo  4. Click "Customize installation"
    echo  5. Make sure "tcl/tk and IDLE" is CHECKED
    echo  6. Complete the install
    echo  7. Run this installer again
    echo.
    pause
    exit /b 1
)

echo.
echo  [OK] Python 3.12 installed with tkinter!

:: ──────────────────────────────────────────────────────
:tkinter_ok
:: ──────────────────────────────────────────────────────
echo  [OK] tkinter available

echo.
echo  [*] Checking dependencies...

:: Install requests if missing
"%PYTHON_EXE%" -c "import requests" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [*] Installing requests library...
    "%PYTHON_EXE%" -m pip install requests --quiet 2>nul
    if %errorlevel% neq 0 (
        "%PYTHON_EXE%" -m pip install requests
    )
)

:: Install pynput for global hotkeys
"%PYTHON_EXE%" -c "import pynput" >nul 2>&1
if %errorlevel% neq 0 (
    echo  [*] Installing pynput (global hotkeys)...
    "%PYTHON_EXE%" -m pip install pynput --quiet 2>nul
    if %errorlevel% neq 0 (
        "%PYTHON_EXE%" -m pip install pynput
    )
)

echo  [OK] All dependencies ready
echo.

:: ── Launch SC_Toolbox ──
echo  =============================================
echo   Launching SC_Toolbox...
echo  =============================================
echo.

set "TOOLBOX_DIR=%~dp0"
"%PYTHON_EXE%" "%TOOLBOX_DIR%skill_launcher.py" 100 100 500 550 0.95 NUL

if %errorlevel% neq 0 (
    echo.
    echo  [!] SC_Toolbox exited with an error (code %errorlevel%).
    echo.
    echo  Common fixes:
    echo  - Make sure no other instance is already running
    echo  - Try running as Administrator
    echo  - Check that skill files exist in the skills\ folder
    echo.
    pause
)
