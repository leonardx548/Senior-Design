@echo off
setlocal

if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe test_stand_app.py
) else (
    py -3 test_stand_app.py
)

if errorlevel 1 pause
