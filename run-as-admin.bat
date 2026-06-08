@echo off
REM Opens an elevated PowerShell already cd'd into this folder, so you can run
REM   python recover.py scan E: --mode all --out E:\recovered
REM against live volumes / physical disks (which require Administrator rights).
set "HERE=%~dp0"
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-NoProfile','-Command',('Set-Location ''%HERE%''; Write-Host ''recolib - elevated shell. Try: python recover.py list'' -ForegroundColor Green')"
