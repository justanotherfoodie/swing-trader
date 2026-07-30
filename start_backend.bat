@echo off
title Trader Backend
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%\backend"

:: Python: prefer the pinned interpreter, otherwise whatever is on PATH.
if not defined PYTHON set "PYTHON=C:\Python314\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo ============================================
echo  Trader Backend - http://localhost:8000
echo ============================================
echo.

echo Using Python: %PYTHON%
"%PYTHON%" --version
if errorlevel 1 (
    echo ERROR: No usable Python found.
    echo   Tried C:\Python314\python.exe and "python" on PATH.
    echo   Install Python 3.11+ or set PYTHON=full\path\to\python.exe before running.
    pause
    exit /b 1
)

echo.
echo Installing dependencies...
"%PYTHON%" -m pip install -r requirements.txt

echo.
echo Starting backend on http://localhost:8000
echo First scan runs automatically (takes ~60 seconds)
echo.
"%PYTHON%" -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause
endlocal
