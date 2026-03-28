@echo off
setlocal enabledelayedexpansion
title SC_Toolbox
cd /d "%~dp0"

:: Try to find Python (same search order as INSTALL_AND_LAUNCH.bat)
set "PY="

:: Standard Python.org installs
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :run
    )
)

:: Winget / package manager installs (two levels deep)
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%~D\python.exe" (
            set "PY=%%~D\python.exe"
            goto :run
        )
        for /d %%E in ("%%~D\*") do (
            if exist "%%~E\python.exe" (
                set "PY=%%~E\python.exe"
                goto :run
            )
        )
    )
)

:: PATH lookup (skip Windows Store)
where python >nul 2>&1
if !errorlevel!==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            set "PY=%%P"
            goto :run
        )
    )
)

:: Program Files (both nested and direct)
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%ProgramFiles%\Python\Python%%V\python.exe" (
        set "PY=%ProgramFiles%\Python\Python%%V\python.exe"
        goto :run
    )
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        set "PY=%ProgramFiles%\Python%%V\python.exe"
        goto :run
    )
)

:: Legacy C:\PythonXX
for %%V in (314 313 312 311 310 39 38) do (
    if exist "C:\Python%%V\python.exe" (
        set "PY=C:\Python%%V\python.exe"
        goto :run
    )
)

:: Last resort: Windows Store python if it works
where python >nul 2>&1
if !errorlevel!==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        "%%P" -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>&1
        if !errorlevel!==0 (
            set "PY=%%P"
            goto :run
        )
    )
)

:: Try Python Launcher (py.exe)
where py >nul 2>&1
if !errorlevel!==0 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if exist "%%P" (
            set "PY=%%P"
            goto :run
        )
    )
)

:: Not found — run full installer which can download and install Python
echo.
echo  Python not found. Running installer (will auto-install Python)...
echo.
call "%~dp0INSTALL_AND_LAUNCH.bat"
exit /b

:run
:: Verify dependencies
set "NEED_INSTALL=0"
"%PY%" -c "import PySide6" >nul 2>&1
if !errorlevel! neq 0 set "NEED_INSTALL=1"
"%PY%" -c "import requests" >nul 2>&1
if !errorlevel! neq 0 set "NEED_INSTALL=1"
"%PY%" -c "import pynput" >nul 2>&1
if !errorlevel! neq 0 set "NEED_INSTALL=1"

if "!NEED_INSTALL!"=="1" (
    echo  Dependencies missing. Installing from requirements.txt...
    "%PY%" -m pip install -r "%~dp0requirements.txt" --quiet 2>nul
    if !errorlevel! neq 0 (
        "%PY%" -m pip install -r "%~dp0requirements.txt"
    )
    "%PY%" -c "import PySide6" >nul 2>&1
    if !errorlevel! neq 0 (
        echo  Dependencies could not be installed.
        echo  Running full installer...
        echo.
        call "%~dp0INSTALL_AND_LAUNCH.bat"
        exit /b
    )
)

"%PY%" "%~dp0skill_launcher.py" 100 100 500 550 0.95 nul
if !errorlevel! neq 0 (
    echo.
    echo  SC_Toolbox exited with an error.
    pause
)
