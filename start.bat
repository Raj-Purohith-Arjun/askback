@echo off
REM Start the dashboard for the HR example. Optional first argument is a port: start.bat 8010
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo  [X] Not set up yet. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

set PORT=%1
if "%PORT%"=="" set PORT=8000

echo.
echo  Starting the dashboard ^(HR example simulator^) on http://localhost:%PORT%
echo  Keep this window open while you use it. Press Ctrl+C to stop.
echo.

REM Give the server a moment to bind before the browser asks for the page.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:%PORT%"

.venv\Scripts\python.exe -m askback --config examples\hr\config.yaml serve --port %PORT%
