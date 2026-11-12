@echo off
setlocal
cd /d "%~dp0"

echo LabJack T4 connection check
echo.
TestStandLabJackDiagnostic.exe
echo.
pause
