@echo off
setlocal enabledelayedexpansion
title SC_Toolbox
cd /d "%~dp0"

:: Find Python — check standard python.org installs first
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :found
    )
)

:: Check winget / package manager installs (two levels deep)
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%~D\python.exe" (
            set "PY=%%~D\python.exe"
            goto :found
        )
        for /d %%E in ("%%~D\*") do (
            if exist "%%~E\python.exe" (
                set "PY=%%~E\python.exe"
                goto :found
            )
        )
    )
)

:: Check Program Files
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        set "PY=%ProgramFiles%\Python%%V\python.exe"
        goto :found
    )
)

:: Fallback: try PATH (skip Windows Store stub)
where python >nul 2>&1
if !errorlevel!==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            set "PY=%%P"
            goto :found
        )
    )
    :: If only WindowsApps python exists, try it anyway
    set "PY=python"
    goto :found
)

:: Try Python Launcher (py.exe)
where py >nul 2>&1
if !errorlevel!==0 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if exist "%%P" (
            set "PY=%%P"
            goto :found
        )
    )
)

echo ERROR: Python not found. Run INSTALL_AND_LAUNCH.bat first,
echo or install Python 3.10+ with PySide6 from https://www.python.org/downloads/
pause
goto :done

:found
echo Found Python: !PY!

:: Verify PySide6 before launching
"!PY!" -c "import PySide6" >nul 2>&1
if !errorlevel! neq 0 (
    echo PySide6 not found. Installing...
    "!PY!" -m pip install "PySide6>=6.5.0" --quiet 2>nul
    if !errorlevel! neq 0 (
        "!PY!" -m pip install "PySide6>=6.5.0"
    )
    "!PY!" -c "import PySide6" >nul 2>&1
    if !errorlevel! neq 0 (
        echo ERROR: PySide6 could not be installed. Run INSTALL_AND_LAUNCH.bat instead.
        pause
        goto :done
    )
)

"!PY!" skill_launcher.py 100 100 500 550 0.95 nul

:done
