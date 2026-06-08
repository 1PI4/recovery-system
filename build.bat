@echo off
REM Build a standalone RecoveryDashboard.exe (no Python needed on the target PC).
REM Result: dist\RecoveryDashboard.exe
cd /d "%~dp0"
python tools\build_exe.py
echo.
pause
