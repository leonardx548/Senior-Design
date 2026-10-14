@echo off
setlocal

if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe labjack_diagnostic.py
) else (
    py -3 labjack_diagnostic.py
)

pause
