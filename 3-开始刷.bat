@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3 first.
  pause
  exit /b 1
)
python farm.py --auto
if errorlevel 3 exit /b
echo.
pause
