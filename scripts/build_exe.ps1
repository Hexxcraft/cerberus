# Builds Cerberus.exe (a folder you can run without Python) and puts a shortcut on the Desktop.
# Run from the project folder:  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# Options: -NoShortcut (CI / no Desktop shortcut)   -Python <path to python.exe> (default: the project's .venv)
param([switch]$NoShortcut, [string]$Python = ".\.venv\Scripts\python.exe")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = $Python
if (($py -like "*\*") -and -not (Test-Path $py)) { throw "Run install.bat first (the .venv folder is missing)." }

& $py -m pip install --quiet pyinstaller
& $py -c "from PIL import Image; Image.open('cerberus/web/icon-512.png').save('assets/cerberus.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

& $py -m PyInstaller --noconfirm --clean --windowed --name Cerberus --icon assets\cerberus.ico `
    --add-data "assets;assets" `
    --add-data "cerberus\web;cerberus\web" `
    --add-data "cerberus\seed;cerberus\seed" `
    --add-data "plugins\_template.py;plugins" `
    --add-data "cerberus\resume_worker.py;cerberus" `
    --collect-data customtkinter `
    --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on `
    --hidden-import apscheduler.triggers.cron --hidden-import apscheduler.triggers.interval `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (see messages above)." }

$exe = Join-Path (Get-Location) "dist\Cerberus\Cerberus.exe"
if (-not (Test-Path $exe)) { throw "Build finished but Cerberus.exe was not found." }
Write-Host "Built: $exe"
if (-not $NoShortcut) { & powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\make_shortcut.ps1" -Target $exe }
