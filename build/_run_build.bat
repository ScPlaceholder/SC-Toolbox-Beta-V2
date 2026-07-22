@echo off
cd /d "C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI\custom_skills\SC_Toolbox_Beta_V1.2\build"
call build_installer.bat velopack
exit /b %errorlevel%
