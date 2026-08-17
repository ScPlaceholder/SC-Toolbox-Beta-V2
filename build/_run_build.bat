@echo off
REM Elah 2026-08-17: was a hardcoded absolute path to one machine's user folder. My privacy
REM scrub rewrote the username inside it, which turned 'works on one machine' into 'works
REM nowhere' -- cd /d is a real dereference, unlike the provenance fields elsewhere.
REM %~dp0 is this script's own directory, so it is correct on every machine including a
REM fresh clone. Strictly better than what was there before the scrub.
cd /d "%~dp0"
call build_installer.bat velopack
exit /b %errorlevel%
