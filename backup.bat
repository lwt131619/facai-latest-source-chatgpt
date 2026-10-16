@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe scripts\backup_local.py %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python scripts\backup_local.py %*
  ) else (
    py -3 scripts\backup_local.py %*
  )
)
set "FACAI_BACKUP_EXIT=%errorlevel%"
if "%~1"=="" pause
exit /b %FACAI_BACKUP_EXIT%
