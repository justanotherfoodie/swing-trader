@echo off
title Trader Dashboard
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%\frontend"

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
call npm install

echo.
echo Starting dashboard on http://localhost:3000
call npm run dev

pause
endlocal
