@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python Launcher was not found. Install Python 3.10 or newer with "Add Python to PATH" selected.
    pause
    exit /b 1
)

py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto :failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release

py -3 -m PyInstaller --noconfirm --clean --windowed --onedir --name TestStand --hidden-import labjack test_stand_app.py
if errorlevel 1 goto :failed

py -3 -m PyInstaller --noconfirm --clean --console --onefile --name TestStandLabJackDiagnostic --hidden-import labjack --distpath dist\TestStand labjack_diagnostic.py
if errorlevel 1 goto :failed

copy /y "README.md" "dist\TestStand\README.md" >nul
copy /y "RELEASE_README.txt" "dist\TestStand\START_HERE.txt" >nul
mkdir release
powershell -NoProfile -Command "Compress-Archive -Path 'dist\TestStand\*' -DestinationPath 'release\SeniorDesignTestStand-v0.3.16-windows.zip' -Force"

echo.
echo Release package created:
echo   release\SeniorDesignTestStand-v0.3.16-windows.zip
pause
exit /b 0

:failed
echo.
echo Release build failed. Review the messages above.
pause
exit /b 1
