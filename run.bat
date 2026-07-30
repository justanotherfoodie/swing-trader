@echo off
title Trader - Starting...
setlocal

:: Repo root = the folder this script lives in (trailing backslash included in %~dp0)
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

:: Python: prefer the pinned interpreter, otherwise whatever is on PATH.
if not defined PYTHON set "PYTHON=C:\Python314\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo ============================================
echo  Swing Trader - Starting all services...
echo ============================================
echo.
echo  Repo:   %ROOT%
echo  Python: %PYTHON%
echo.

:: Sanity check the interpreter before spawning windows that would just flash and die.
"%PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: No usable Python found.
    echo   Tried C:\Python314\python.exe and "python" on PATH.
    echo   Install Python 3.11+ or set PYTHON=full\path\to\python.exe before running.
    pause
    exit /b 1
)

:: Kill anything already on port 8000 or 3000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " 2^>nul') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":3000 " 2^>nul') do taskkill /PID %%a /F >nul 2>&1

:: Start backend in background window
echo [1/3] Starting backend...
:: /d sets the new window's working directory, so no nested-quote "cd &&" needed.
start "Trader Backend" /d "%ROOT%\backend" cmd /k "%PYTHON%" -m uvicorn main:app --host 0.0.0.0 --port 8000

:: Start frontend in background window
echo [2/3] Starting frontend...
start "Trader Frontend" /d "%ROOT%\frontend" cmd /k npm run dev

:: Wait for frontend to be ready then open browser
echo [3/3] Waiting for dashboard to be ready...
timeout /t 8 /nobreak >nul

:: Open browser
echo Opening http://localhost:3000 ...
start "" "http://localhost:3000"

echo.
echo ============================================
echo  Done! Dashboard open in your browser.
echo  Close the two terminal windows to stop.
echo ============================================
echo.
pause
endlocal
