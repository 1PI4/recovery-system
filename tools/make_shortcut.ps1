# Create a Desktop shortcut to the built RecoveryDashboard.exe (with its icon).
param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$exe = Join-Path $Root 'dist\RecoveryDashboard.exe'
if (-not (Test-Path $exe)) {
    Write-Host "Build the exe first (build.bat / python tools\build_exe.py)." -ForegroundColor Yellow
    exit 1
}
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'recolib Data Recovery.lnk'
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $exe
$sc.WorkingDirectory = (Split-Path $exe)
$sc.IconLocation = "$exe,0"
$sc.Description = 'recolib Data Recovery'
$sc.Save()
Write-Host "Shortcut created: $lnk" -ForegroundColor Green
