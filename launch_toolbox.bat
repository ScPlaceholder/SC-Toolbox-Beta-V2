@echo off
setlocal
title SC_Toolbox
cd /d "%~dp0"

:: Find Python — check standard python.org installs first
for %%V in (314 313 312 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        echo Found Python: %LOCALAPPDATA%\Programs\Python\Python%%V\python.exe
        "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" skill_launcher.py 100 100 500 550 0.95 NUL
        goto :done
    )
)

:: Check winget / package manager installs (two levels deep)
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%~D\python.exe" (
            echo Found Python: %%~D\python.exe
            "%%~D\python.exe" skill_launcher.py 100 100 500 550 0.95 NUL
            goto :done
        )
        for /d %%E in ("%%~D\*") do (
            if exist "%%~E\python.exe" (
                echo Found Python: %%~E\python.exe
                "%%~E\python.exe" skill_launcher.py 100 100 500 550 0.95 NUL
                goto :done
            )
        )
    )
)

:: Check Program Files
for %%V in (314 313 312 311 310) do (
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        echo Found Python: %ProgramFiles%\Python%%V\python.exe
        "%ProgramFiles%\Python%%V\python.exe" skill_launcher.py 100 100 500 550 0.95 NUL
        goto :done
    )
)

:: Fallback: try PATH (skip Windows Store stub)
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            echo Using Python from PATH: %%P
            "%%P" skill_launcher.py 100 100 500 550 0.95 NUL
            goto :done
        )
    )
    :: If only WindowsApps python exists, try it anyway
    echo Using Python from PATH
    python skill_launcher.py 100 100 500 550 0.95 NUL
    goto :done
)

:: Try Python Launcher (py.exe)
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    echo Using Python via py launcher
    py -3 skill_launcher.py 100 100 500 550 0.95 NUL
    goto :done
)

echo ERROR: Python not found. Run INSTALL_AND_LAUNCH.bat first,
echo or install Python 3.10+ from https://www.python.org/downloads/
pause

:done
