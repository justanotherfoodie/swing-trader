@echo off
title Trader Backend
cd /d "C:\Users\gongy\OneDrive\Desktop\Trader\backend"

set PYTHON=C:\Python314\python.exe

echo ============================================
echo  Trader Backend - http://localhost:8000
echo ============================================
echo.

echo Using Python: %PYTHON%
%PYTHON% --version
if errorlevel 1 (
    echo ERROR: Python 3.14 not found at expected path.
    pause
    exit /b 1
)

echo.
echo Installing dependencies...
%PYTHON% -m pip install fastapi uvicorn yfinance pandas numpy anthropic httpx python-dotenv apscheduler requests beautifulsoup4 2>&1

echo.
echo Starting backend on http://localhost:8000
echo First scan runs automatically (takes ~60 seconds)
echo.
%PYTHON% -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause
