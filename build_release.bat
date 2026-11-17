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

%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 goto :failed

%PYTHON_CMD% -m pip install pyinstaller
if errorlevel 1 goto :failed

%PYTHON_CMD% -m unittest -v test_core.py
if errorlevel 1 goto :failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release

%PYTHON_CMD% -m PyInstaller --noconfirm --clean --windowed --onedir --name TestStand --collect-all labjack test_stand_app.py
if errorlevel 1 goto :failed

%PYTHON_CMD% -m PyInstaller --noconfirm --clean --console --onefile --name TestStandLabJackDiagnostic --collect-all labjack --distpath dist\TestStand labjack_diagnostic.py
if errorlevel 1 goto :failed

copy /y "README.md" "dist\TestStand\README.md" >nul
copy /y "RELEASE_README.txt" "dist\TestStand\START_HERE.txt" >nul
copy /y "Verify LabJack.bat" "dist\TestStand\Verify LabJack.bat" >nul
copy /y "Install LabJack Driver.url" "dist\TestStand\Install LabJack Driver.url" >nul
mkdir release
%PYTHON_CMD% -c "from pathlib import Path; import shutil; source = Path('dist/TestStand'); target = Path('release/SeniorDesignTestStand-v0.4.4-windows.zip'); shutil.make_archive(str(target.with_suffix('')), 'zip', root_dir=source)"
if errorlevel 1 goto :failed

echo.
echo Release package created:
echo   release\SeniorDesignTestStand-v0.4.4-windows.zip
pause
exit /b 0

:failed
echo.
echo Release build failed. Review the messages above.
pause
exit /b 1
