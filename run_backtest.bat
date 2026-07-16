@echo off
title Options Backtester
cd /d "C:\Users\gongy\OneDrive\Desktop\Trader\backend"

set PYTHON=C:\Python314\python.exe

echo ============================================
echo  Options Strategy Backtester
echo ============================================
echo.
echo Simulates trading the engine's call/put spreads with $600
echo over a ~1 week hold, using REAL historical prices.
echo.
echo (This takes ~30 seconds to scan the market...)
echo.

%PYTHON% backtester.py %*

echo.
echo ============================================
echo  Tip: customize it, e.g.
echo    run_backtest.bat --budget 1000 --hold-days 10
echo    run_backtest.bat --as-of 2026-06-09
echo ============================================
pause
