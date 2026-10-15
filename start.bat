@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python scripts\start.py %*
) else (
  py -3 scripts\start.py %*
)
if errorlevel 1 pause
