@echo off
REM Elah 2026-08-17. Two fixes, both found by testing rather than guessing.
REM 1. This used to cd to a hardcoded absolute path under one user's profile. The privacy
REM    scrub rewrote the username inside it, turning 'works on one machine' into 'works
REM    nowhere' -- cd /d is a real dereference, unlike the provenance strings elsewhere.
REM 2. The CALL must use an explicit %~dp0 path. A bare 'call build_installer.bat' returns
REM    errorlevel 1 with 'is not recognized' EVEN THOUGH cd /d succeeded and 'if exist'
REM    finds the file in that same directory. Measured side by side: relative rc=1,
REM    explicit rc=0 and the build proceeds. So do not 'simplify' this back.
cd /d "%~dp0"
call "%~dp0build_installer.bat" velopack
exit /b %errorlevel%
