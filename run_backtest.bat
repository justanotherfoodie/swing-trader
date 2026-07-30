@echo off
title Options Backtester
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%\backend"

:: Python: prefer the pinned interpreter, otherwise whatever is on PATH.
if not defined PYTHON set "PYTHON=C:\Python314\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: No usable Python found.
    echo   Tried C:\Python314\python.exe and "python" on PATH.
    echo   Install Python 3.11+ or set PYTHON=full\path\to\python.exe before running.
    pause
    exit /b 1
)

echo ============================================
echo  Options Strategy Backtester
echo ============================================
echo.
echo Simulates trading the engine's call/put spreads with $600
echo over a ~1 week hold, using REAL historical prices.
echo.
echo (This takes ~30 seconds to scan the market...)
echo.

"%PYTHON%" backtester.py %*

echo.
echo ============================================
echo  Tip: customize it, e.g.
echo    run_backtest.bat --budget 1000 --hold-days 10
echo    run_backtest.bat --as-of 2026-06-09
echo.
echo  For the walk-forward test (the honest one):
echo    "%PYTHON%" walkforward.py --years 3 --hold-days 5
echo ============================================
pause
endlocal
