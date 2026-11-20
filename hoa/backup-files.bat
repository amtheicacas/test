@echo off
rem Copies every uploaded file from the live bucket to the backup bucket (copy only; never deletes or overwrites).
rem Uses local.live.env, which needs the STORAGE_... lines (live bucket) AND the BACKUP_... lines (backup bucket).
rem   backup-files.bat              does the copy
rem   backup-files.bat --dry-run    only shows what it would copy
cd /d "%~dp0"
if not exist local.live.env (
  echo local.live.env was not found in this folder. See the instructions.
  pause
  exit /b 1
)
set HOA_ENV_FILE=%~dp0local.live.env
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% manage.py backup_files %*
echo.
echo Finished. You can close this window.
pause
