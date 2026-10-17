# Puts a "Cerberus" shortcut on the Desktop.
# With no -Target it points at run_cerberus.bat (works from source); build_exe.ps1 passes Cerberus.exe instead.
param([string]$Target = "")
$root = Split-Path $PSScriptRoot -Parent
if (-not $Target) { $Target = Join-Path $root "run_cerberus.bat" }
$icon = Join-Path $root "assets\cerberus.ico"
if (-not (Test-Path $icon)) {
    & (Join-Path $root ".venv\Scripts\python.exe") -c "import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2], sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])" (Join-Path $root "cerberus\web\icon-512.png") $icon
}
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Cerberus.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = $Target
$s.WorkingDirectory = Split-Path $Target -Parent
if (Test-Path $icon) { $s.IconLocation = $icon }
$s.Description = "Cerberus - the three-headed guardian"
$s.Save()
Write-Host "Shortcut created: $lnk"
