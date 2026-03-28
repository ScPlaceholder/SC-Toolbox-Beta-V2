@echo off
setlocal enabledelayedexpansion
title SC_Toolbox DEBUG Launch
color 0B

set "TRACE_LOG=%~dp0logs\debug_trace.log"
echo [%TIME%] DEBUG_LAUNCH started > "%TRACE_LOG%"

echo.
echo  [DEBUG] Step 1: Finding Python...
echo [%TIME%] Step 1: Finding Python >> "%TRACE_LOG%"

set "PYTHON_EXE="

:: Quick search — winget path (what the launcher log shows)
if exist "%LOCALAPPDATA%\Python" (
    for /d %%D in ("%LOCALAPPDATA%\Python\*") do (
        if exist "%%~D\python.exe" (
            set "PYTHON_EXE=%%~D\python.exe"
            goto :got_py
        )
        for /d %%E in ("%%~D\*") do (
            if exist "%%~E\python.exe" (
                set "PYTHON_EXE=%%~E\python.exe"
                goto :got_py
            )
        )
    )
)

:: Standard paths
for %%V in (314 313 312 311 310 39 38) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :got_py
    )
)

echo  [!] Python not found.
echo [%TIME%] FAILED: Python not found >> "%TRACE_LOG%"
goto :done

:got_py
echo  [OK] Python: %PYTHON_EXE%
echo [%TIME%] Step 1 OK: %PYTHON_EXE% >> "%TRACE_LOG%"

echo.
echo  [DEBUG] Step 2: Checking PySide6...
echo [%TIME%] Step 2: Checking PySide6 >> "%TRACE_LOG%"

"%PYTHON_EXE%" -c "import PySide6; print('PySide6 OK')" 2>&1
echo [%TIME%] Step 2 errorlevel: !errorlevel! >> "%TRACE_LOG%"

echo.
echo  [DEBUG] Step 3: Launching skill_launcher.py...
echo [%TIME%] Step 3: Launching >> "%TRACE_LOG%"

set "TOOLBOX_DIR=%~dp0"
echo  [DEBUG] Command: "%PYTHON_EXE%" "%TOOLBOX_DIR%skill_launcher.py" 100 100 500 550 0.95 nul
echo [%TIME%] Command: "%PYTHON_EXE%" "%TOOLBOX_DIR%skill_launcher.py" 100 100 500 550 0.95 nul >> "%TRACE_LOG%"

"%PYTHON_EXE%" "%TOOLBOX_DIR%skill_launcher.py" 100 100 500 550 0.95 nul
set "EC=!errorlevel!"

echo.
echo  [DEBUG] Step 4: Launcher exited with code !EC!
echo [%TIME%] Step 4: exit code !EC! >> "%TRACE_LOG%"

:done
echo.
echo [%TIME%] Reached :done >> "%TRACE_LOG%"
echo  =============================================
echo   DEBUG complete. Check logs\debug_trace.log
echo  =============================================
echo.
echo  Press any key to close...
pause >nul
