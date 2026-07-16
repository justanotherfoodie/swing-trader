# Swing Trader

A US-market swing-trade scanner with a defined-risk options workflow.

Scans the S&P 500 + Nasdaq-100 + S&P MidCap 400 (~900 tickers), runs 5 technical
strategies (RSI mean reversion, MACD+RSI confluence, EMA crossover, Bollinger Band
squeeze breakout, EMA50+BB trend) with a macro news-sentiment overlay, and produces
BUY/SELL/WATCH signals with entry, stop-loss, and take-profit levels.

For each signal it can also build a **defined-risk options spread** (bull call spread
or bear put spread) priced from live option chain data, and a guided workflow that
allocates a budget across positions, tracks them, and tells you when to sell.

## Stack

- **Backend:** Python (FastAPI) — `yfinance` for price/options data, `pandas`/`numpy`
  for indicators, an LLM API for news sentiment and trade rationale text.
- **Frontend:** Next.js 14 + TypeScript, Tailwind, dark dashboard UI.

## Structure

```
backend/
  main.py              FastAPI app + REST endpoints
  scanner.py            Market scan orchestration
  backtester.py         Backtest the options strategy over a historical window
  portfolio.py           Budget -> plan -> open positions -> HOLD/SELL verdicts
  data/                 Price/news/universe fetchers
  signals/               Indicators, the 5 strategies, scorer, options pricing
  ai/                    LLM-based sentiment + rationale generation
frontend/
  app/                   Next.js pages
  components/            Dashboard UI (signal cards, macro bar, options planner)
  lib/                   API client
```

## Setup

1. Install backend deps: `pip install -r backend/requirements.txt`
2. Install frontend deps: `cd frontend && npm install`
3. Copy `.env.example` to `backend/.env` and fill in your API keys:
   - `ANTHROPIC_API_KEY` — for AI sentiment/rationale (optional; falls back to
     template-based reasons if omitted)
   - `NEWS_API_KEY` — for ticker/macro news (optional; falls back to Yahoo Finance
     headlines if omitted)
4. Run `run.bat` (Windows) to start both servers and open the dashboard, or run
   `uvicorn main:app` in `backend/` and `npm run dev` in `frontend/` manually.

## Backtesting

```
python backend/backtester.py --budget 600 --hold-days 5
```

Simulates trading the options strategy over a historical window using real prices,
with option premiums modeled via Black-Scholes (no historical option chain data is
available from free sources).

## Disclaimer

For personal, educational use. Not financial advice. Trading involves risk of loss.
