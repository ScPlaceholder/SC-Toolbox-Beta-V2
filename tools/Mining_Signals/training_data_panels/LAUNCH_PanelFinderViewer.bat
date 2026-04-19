@echo off
REM Live viewer for the panel-finder diagnostic overlay.
REM Shows in real time exactly where the OCR pipeline is locating
REM HUD lines, the mineral name band, the MASS/RESIST/INSTAB rows,
REM and where each value crop is being grabbed from.
cd /d "%~dp0\.."
start "" "C:\Users\prjgn\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" "scripts\live_panel_finder_viewer.py"
