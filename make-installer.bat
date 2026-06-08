@echo off
REM Build the Windows installer (Setup.exe) via Inno Setup.
REM Output: installer_output\recolib-Recovery-Setup.exe
cd /d "%~dp0"
python tools\build_installer.py
echo.
pause
