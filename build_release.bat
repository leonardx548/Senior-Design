@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD=py -3"
py -3 --version >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or newer was not found. Install it with "Add Python to PATH" selected.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release

%PYTHON_CMD% -m PyInstaller --noconfirm --clean --windowed --onedir --name TestStand --hidden-import labjack test_stand_app.py
if errorlevel 1 goto :failed

%PYTHON_CMD% -m PyInstaller --noconfirm --clean --console --onefile --name TestStandLabJackDiagnostic --hidden-import labjack --distpath dist\TestStand labjack_diagnostic.py
if errorlevel 1 goto :failed

copy /y "README.md" "dist\TestStand\README.md" >nul
copy /y "RELEASE_README.txt" "dist\TestStand\START_HERE.txt" >nul
mkdir release
powershell -NoProfile -Command "Compress-Archive -Path 'dist\TestStand\*' -DestinationPath 'release\SeniorDesignTestStand-v0.3.21-windows.zip' -Force"

echo.
echo Release package created:
echo   release\SeniorDesignTestStand-v0.3.21-windows.zip
pause
exit /b 0

:failed
echo.
echo Release build failed. Review the messages above.
pause
exit /b 1
