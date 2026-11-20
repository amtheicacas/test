@echo off
rem Checks a file bucket using the values in a Notepad file that is never saved to GitHub.
rem   check-live-storage.bat                       uses local.live.env  (the live Backblaze bucket)
rem   check-live-storage.bat local.r2.env          uses local.r2.env    (the Cloudflare R2 backup bucket)
rem Nothing is sent to anyone and no case data is touched: it writes, reads back and tests a small file.
cd /d "%~dp0"
set ENVNAME=%~1
if "%ENVNAME%"=="" set ENVNAME=local.live.env
if not exist "%ENVNAME%" (
  echo %ENVNAME% was not found in this folder. Make it in Notepad first, see the instructions.
  pause
  exit /b 1
)
set HOA_ENV_FILE=%~dp0%ENVNAME%
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% manage.py check_storage
echo.
echo Finished. You can close this window.
pause
