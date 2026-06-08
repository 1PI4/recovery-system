@echo off
REM Launch the recolib recovery dashboard (GUI).
REM Tip: to scan live disks, right-click this file -> "Run as administrator",
REM or use the "Restart as Admin" button inside the dashboard.
cd /d "%~dp0"
python dashboard.py
if errorlevel 1 pause
