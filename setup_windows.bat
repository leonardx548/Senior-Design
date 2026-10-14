@echo off
setlocal

where py >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer, then run this file again.
    pause
    exit /b 1
)

echo Creating the project virtual environment...
py -3 -m venv .venv
if errorlevel 1 (
    echo Failed to create .venv.
    pause
    exit /b 1
)

echo Installing Python dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo Setup complete.
echo Install the official LabJack LJM Windows driver before using live T4 hardware.
echo Then run run_diagnostic.bat, followed by run_test_stand.bat.
pause
