@echo off
rem One-time setup: creates a private Python environment (.venv) and installs Cerberus' packages.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the environment. Install Python 3.11 or newer from python.org and try again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Package install failed. See the messages above.
  pause
  exit /b 1
)
echo.
echo Done. Start Cerberus with run_cerberus.bat
pause
