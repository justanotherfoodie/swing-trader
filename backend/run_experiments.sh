#!/usr/bin/env bash
# Pre-registered experiments. Each hypothesis was stated BEFORE seeing its result,
# and each is judged on out-of-sample performance, not the full-sample number.
#
#  E1  Drop MACD+RSI Confluence   (worst strategy: -$12.91/trade over 61 trades)
#  E2  Take only "medium" quality (the "high" grade underperformed it)
#  E3  Widen the stop to -60%     (stops: 24 trades, 0% win, -$1,433)
#
# Anything that improves in-sample but not out-of-sample is noise, and gets rejected.
PY="C:/Python314/python.exe"
ARGS="--years 3 --universe 250 --step 3 --picks 3 --split"

echo "=== E1: exclude MACD+RSI ==="
$PY -W ignore walkforward.py $ARGS --exclude macd_rsi > exp_e1_no_macd.txt 2>&1
echo "E1 done"

echo "=== E2: medium quality only ==="
$PY -W ignore walkforward.py $ARGS --min-quality medium > exp_e2_medium.txt 2>&1
echo "E2 done"

echo "=== E3: wider stop (-60%) ==="
$PY -W ignore walkforward.py $ARGS --stop-pct 0.6 > exp_e3_widestop.txt 2>&1
echo "E3 done"

echo "ALL EXPERIMENTS COMPLETE"
