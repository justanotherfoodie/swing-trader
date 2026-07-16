@echo off
title Trader Dashboard
cd /d "C:\Users\gongy\OneDrive\Desktop\Trader\frontend"

echo ============================================
echo  Trader Dashboard - http://localhost:3000
echo ============================================
echo.
echo Make sure start_backend.bat is already running first!
echo.

node --version
if errorlevel 1 (
    echo ERROR: Node.js not found. Install from nodejs.org
    pause
    exit /b 1
)

echo.
echo Installing packages (first time only)...
npm install

echo.
echo Starting dashboard on http://localhost:3000
npm run dev

pause
