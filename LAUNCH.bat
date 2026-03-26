@echo off
setlocal
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

:: Winget / package manager installs
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%D\python.exe" (
            set "PY=%%D\python.exe"
            goto :run
        )
    )
)

:: PATH lookup (skip Windows Store)
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            set "PY=%%P"
            goto :run
        )
    )
)

:: Program Files
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%ProgramFiles%\Python\Python%%V\python.exe" (
        set "PY=%ProgramFiles%\Python\Python%%V\python.exe"
        goto :run
    )
)

:: Not found
echo.
echo  Python not found. Running installer...
echo.
call "%~dp0INSTALL_AND_LAUNCH.bat"
exit /b

:run
:: Verify tkinter
"%PY%" -c "import tkinter" >nul 2>&1
if %errorlevel% neq 0 (
    echo  Python found but tkinter is missing.
    echo  Running installer to fix...
    echo.
    call "%~dp0INSTALL_AND_LAUNCH.bat"
    exit /b
)

"%PY%" "%~dp0skill_launcher.py" 100 100 500 550 0.95 NUL
if %errorlevel% neq 0 (
    echo.
    echo  SC_Toolbox exited with an error.
    pause
)
