@echo off
rem Starts the HOA test site. Double-click this file. Leave the black window open while you use the site; close it to stop.
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo The .venv folder was not found. Open PowerShell in this folder and run:  python -m venv .venv
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
set DJANGO_DEBUG=1
set MFA_REQUIRED_ROLES=none
echo Updating the database if needed...
python manage.py migrate
if errorlevel 1 (
  echo.
  echo Something went wrong above. Copy the red/error text and send it to me.
  pause
  exit /b 1
)
echo.
echo The site is starting. When you see "Starting development server", open:  http://127.0.0.1:8000/
echo (Keep this window open. Close it, or press Ctrl+C, to stop the site.)
echo.
start "" cmd /c "timeout /t 5 >nul & start http://127.0.0.1:8000/"
python manage.py runserver
echo.
echo The site has stopped. If it stopped by itself, copy the text above and send it to me.
pause
