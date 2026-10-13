@echo off
REM One-time setup for Windows. Creates a private .venv in this folder and
REM installs the project into it. Nothing is installed system-wide.
setlocal
cd /d "%~dp0"

echo.
echo  askback - setup
echo  =================
echo.

REM --- 1. Is Python present and new enough? --------------------------------- #
where python >nul 2>&1
if errorlevel 1 (
    echo  [X] Python was not found.
    echo.
    echo      Install Python 3.11 or newer from https://www.python.org/downloads/
    echo      IMPORTANT: tick "Add Python to PATH" on the first installer screen,
    echo      then close this window and run setup.bat again.
    echo.
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set FOUND=%%v
    echo  [X] Python %FOUND% is too old. This project needs 3.11 or newer.
    echo      Get it from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
echo  [1/3] Python %PYVER% found.

REM --- 2. Virtual environment ----------------------------------------------- #
if exist ".venv\Scripts\python.exe" (
    echo  [2/3] Reusing the existing .venv folder.
) else (
    echo  [2/3] Creating .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo  [X] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

REM --- 3. Dependencies ------------------------------------------------------- #
echo  [3/3] Installing packages ^(needs internet, takes 1-3 minutes^) ...
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -c constraints.txt -e ".[all]"
if errorlevel 1 (
    echo.
    echo  [X] Install failed. The usual causes are no internet access or a
    echo      corporate proxy. If you use a proxy, set it first:
    echo          set HTTPS_PROXY=http://your-proxy:port
    echo      then run setup.bat again.
    echo.
    pause
    exit /b 1
)

echo.
echo  Setup complete.
echo.
echo    verify.bat   check everything works
echo    start.bat    launch the dashboard for the HR example at http://localhost:8000
echo.
pause
