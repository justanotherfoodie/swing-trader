@echo off
title Trader - Starting...
cd /d "C:\Users\gongy\OneDrive\Desktop\Trader"

set PYTHON=C:\Python314\python.exe

echo ============================================
echo  Swing Trader - Starting all services...
echo ============================================
echo.

:: Kill anything already on port 8000 or 3000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " 2^>nul') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":3000 " 2^>nul') do taskkill /PID %%a /F >nul 2>&1

:: Start backend in background window
echo [1/3] Starting backend...
start "Trader Backend" cmd /k "cd /d C:\Users\gongy\OneDrive\Desktop\Trader\backend && C:\Python314\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000"

:: Start frontend in background window
echo [2/3] Starting frontend...
start "Trader Frontend" cmd /k "cd /d C:\Users\gongy\OneDrive\Desktop\Trader\frontend && npm run dev"

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
