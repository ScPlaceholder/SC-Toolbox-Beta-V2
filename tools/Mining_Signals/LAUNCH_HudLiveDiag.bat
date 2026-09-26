@echo off
REM HUD finder stability diagnostic -> debug_glyphs\hud_live_diag_mosaic.png
REM Set REGION to YOUR HUD region from a debug log line, e.g.:
REM   "_find_label_rows: entry ... region={'x':1311,'y':705,'w':507,'h':320}"
cd /d "%~dp0"
set REGION=1311,705,507,320
set PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe
echo Using Python: %PY%
echo Region: %REGION%   (edit this .bat if wrong)
echo Capturing ~12s — the game HUD must be visible. Each scan is slow; please wait...
"%PY%" "scripts\hud_live_diag.py" --region %REGION% --seconds 12 --hz 4
echo.
echo Mosaic + report are in:  %~dp0debug_glyphs\
echo.
pause
