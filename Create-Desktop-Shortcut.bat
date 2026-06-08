@echo off
REM Put a "recolib Data Recovery" shortcut on your Desktop (points at the .exe).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\make_shortcut.ps1"
pause
