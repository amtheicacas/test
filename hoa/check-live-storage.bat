@echo off
rem Checks the LIVE file bucket using the values in local.live.env (a file you make in Notepad; never saved to GitHub).
rem Nothing is sent to anyone and no case data is touched: it writes, reads back and tests a small file.
cd /d "%~dp0"
if not exist local.live.env (
  echo local.live.env was not found in this folder. Make it in Notepad first, see the instructions.
  pause
  exit /b 1
)
set HOA_ENV_FILE=%~dp0local.live.env
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% manage.py check_storage
echo.
echo Finished. You can close this window.
pause
