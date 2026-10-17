@echo off
rem Starts Cerberus so other devices on your home Wi-Fi can use it.
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\start_lan_server.py
pause
