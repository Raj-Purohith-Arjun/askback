@echo off
REM Prove the install works: unit tests, the built-in example suite, then the HR example.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo  [X] Not set up yet. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

echo.
echo  1. Unit tests
echo  -------------
.venv\Scripts\python.exe -m pytest -q -p no:warnings
if errorlevel 1 (
    echo.
    echo  [X] Unit tests failed. Something is wrong with the install.
    pause
    exit /b 1
)

echo.
echo  2. The built-in example suite ^(must pass^)
echo  -----------------------------------------
.venv\Scripts\python.exe -m askback eval --quiet --format json,html --name verify
if errorlevel 1 (
    echo.
    echo  [X] The built-in example suite failed. It is meant to pass on a working install.
    pause
    exit /b 1
)

echo.
echo  3. The HR example ^(red on purpose^)
echo  ----------------------------------
.venv\Scripts\python.exe -m askback --config examples\hr\config.yaml eval --quiet --format json,html --name verify-hr
if not errorlevel 1 (
    echo.
    echo  [!] Expected the HR example to exit 1 ^(it grades a simulator with known flaws^),
    echo      but it passed. See examples\hr\README.md.
)

echo.
echo  What you should see:
echo    1. every test passes;
echo    2. the example suite passes, so the tool works;
echo    3. the HR example FAILS some cases and exits 1. That is the tool catching the
echo       simulator's real flaws, not a broken install: examples\hr\README.md says which.
echo.
echo  Reports: reports\verify.html ^(example suite^), reports\verify-hr.html ^(HR example^)
echo.
pause
