# Swing Trader

A US-market swing-trade scanner with a defined-risk options workflow, plus the
backtesting machinery to check whether any of it actually makes money.

It scans ~900 tickers (S&P 500 + Nasdaq-100 + S&P MidCap 400), scores each with
several technical strategies under a macro news-sentiment overlay, and produces
BUY / SELL / WATCH signals with entry, stop-loss and take-profit levels. For each
signal it can price a concrete options trade (long call/put, bull call / bear put
debit spread, or a credit spread) from live chain data, allocate a budget across
positions, track them, and tell you when to sell.

---

## Read this before you use it

This section is the honest state of the product. It is not a disclaimer bolted on
the end; it is the most important thing in this file.

**1. The walk-forward backtest found no edge.** Rolling entry dates through three
years of history, with no look-ahead, replaying the real exit ladder, and charging
realistic commission and slippage, the baseline configuration produced **no edge
after costs over 86 trades**. Costs were not a rounding error; they were frequently
the entire result.

**2. The one positive result is small-sample and fragile.** Three changes were
pre-registered and tested. Only removing the MACD+RSI Confluence strategy improved
both the in-sample and out-of-sample halves (in-sample -8.70 to -3.23, out-of-sample
+6.85 to +25.76, combined -3.27 to +6.76 per trade), and it is now disabled by
default. Two other candidates — filtering to medium-quality signals, and widening
the stop — improved one half and hurt the other, and were rejected as overfitting.
Expectancy turning positive after disabling one strategy on a sample this size is
weak evidence, not a validated edge. Treat it as such.

**3. The PDT rule caps a small US account at 3 day trades per 5 business days.**
Under $25,000 in equity, a US margin account that exceeds three day trades in a
rolling five-business-day window gets flagged as a pattern day trader and
restricted. The momentum scanner surfaces 1-3 day setups; that limit is real and
the app cannot get around it.

**4. At a ~$600 account, one option contract risks ~17% of the account.** The risk
engine sizes at 1-2% of equity, which is the professional standard. On $600 that is
$12 per trade — less than the cost of the cheapest realistically liquid contract
(~$100). Buying a single contract therefore risks roughly 17% of the account, not
1-2%. Trading options at textbook risk levels needs on the order of $5,000. The app
states this conflict rather than hiding it (`GET /api/risk` returns `undersized`
and `implied_risk_pct`), but stating it does not fix it.

**5. Not financial advice.** Personal, educational software. Long options can and
regularly do lose 100% of the premium paid. Options pricing in backtests uses
Black-Scholes with trailing realized volatility, because free data has no historical
option chains; it captures direction and time decay but **not** IV expansion or
crush. Trade real money at your own risk.

---

## Stack

- **Backend:** Python + FastAPI. `yfinance` for price and options-chain data,
  `pandas`/`numpy` for indicators, APScheduler for periodic scans, optional
  Anthropic API for news sentiment and rationale text.
- **Frontend:** Next.js 14 + TypeScript, Tailwind, dark dashboard UI.
- Developed against Python 3.14 on Windows; the Docker image uses Python 3.12 and
  the source contains no 3.13/3.14-only syntax.

## Layout

```
backend/
  main.py           FastAPI app + REST endpoints
  config.py         Central settings, read from the environment
  scanner.py        Market scan orchestration
  portfolio.py      Budget -> plan -> open positions -> HOLD/SCALE/SELL verdicts
  risk.py           Account-level risk: % sizing, portfolio heat, circuit breaker
  backtester.py     Single-window backtest ("what happened that week")
  walkforward.py    Walk-forward backtest ("is there an edge at all")
  run_experiments.sh  Pre-registered strategy experiments
  data/             Price / news / universe fetchers (with TTL caches)
  signals/          Indicators, strategies, scorer, options pricing, market context
  ai/               LLM sentiment + rationale generation
frontend/
  app/              Next.js pages
  components/       Signal cards, macro bar, options planner, risk + alerts panels
  lib/              API client
Dockerfile          Multi-target: `backend` and `frontend`
docker-compose.yml  Both services + a volume for trade history
run.bat             Start everything on Windows and open the dashboard
```

## The strategy engine, honestly

Five technical strategies score each ticker; their scores are combined and graded
into a quality tier, then adjusted by a macro sentiment overlay:

| Strategy | Idea | Status |
|---|---|---|
| RSI mean reversion | Fade oversold/overbought extremes | active |
| MACD + RSI confluence | Momentum crossover confirmed by RSI | **disabled by default** — worst performer in walk-forward (-$12.91/trade over 61 trades) |
| EMA crossover | Trend following | active |
| Bollinger squeeze breakout | Volatility contraction then expansion | active |
| EMA50 + Bollinger trend | Trend continuation | active |

These are standard, widely-known indicators computed from free delayed data. There
is no proprietary signal here and nothing in the backtest evidence suggests the
combination beats costs reliably. The genuinely useful parts of this codebase are
the parts that stop you losing money quickly: percentage-of-equity sizing, the
portfolio heat cap, the drawdown circuit breaker, the profit ladder, and a
walk-forward test honest enough to return a negative verdict.

---

## Setup — Windows (native)

Prerequisites: Python 3.11+ (3.14 is what this was developed on) and Node.js 18+.

```bat
git clone <repo> Trader
cd Trader
copy .env.example backend\.env      :: then fill in optional keys
run.bat
```

`run.bat` kills anything already listening on ports 8000 and 3000, starts the
backend and frontend in separate windows, waits, and opens
<http://localhost:3000>.

The launchers resolve the repo location from their own path (`%~dp0`), so the
folder can live anywhere. They use `C:\Python314\python.exe` if it exists and fall
back to `python` on PATH otherwise. To force a specific interpreter:

```bat
set PYTHON=C:\path\to\python.exe
run.bat
```

Individual scripts:

| Script | Does |
|---|---|
| `run.bat` | Everything: both servers + browser |
| `start_backend.bat` | Installs `backend/requirements.txt`, runs uvicorn with `--reload` |
| `start_frontend.bat` | `npm install` then `npm run dev` |
| `run_backtest.bat` | Single-window options backtest (passes through CLI args) |

Manual equivalent:

```bat
pip install -r backend/requirements.txt
cd backend && python -m uvicorn main:app --host 0.0.0.0 --port 8000
cd frontend && npm install && npm run dev
```

## Setup — Docker

```bash
cp .env.example .env          # optional keys; never commit the filled-in copy
docker compose up --build
# dashboard: http://localhost:3000   API: http://localhost:8000
```

Notes:

- **No secrets in the image.** Keys are passed as environment variables at run
  time from the host `.env`; `.dockerignore` keeps every `.env` out of the build
  context.
- **Trade history survives rebuilds.** The named volume `portfolio-data` is mounted
  at `/data`, and `/app/portfolio.json` is a symlink onto it. `docker compose down`
  keeps the volume; `docker compose down -v` deletes it (and your trade history).
- **Why `NEXT_PUBLIC_API_URL=http://localhost:8000`.** `frontend/next.config.js`
  rewrites `/api/*` to `http://localhost:8000`, which does not resolve to the
  backend from inside the frontend container. Compose therefore builds the frontend
  with an absolute API base so the browser calls the published backend port
  directly. That origin is already in the default `CORS_ORIGINS`.
- The backend has a healthcheck on `GET /health`.

Build a single target by hand:

```bash
docker build --target backend  -t trader-backend  .
docker build --target frontend -t trader-frontend .
```

---

## Configuration

All settings live in `backend/config.py`, read from the environment with defaults,
and are exposed as a frozen `settings` object. `.env` is loaded from `backend/.env`
first, then the repo root. Precedence: real env var > `backend/.env` > root `.env`
> default.

```bash
python backend/config.py     # prints the resolved config with secrets redacted
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | Optional. AI sentiment + rationale. Absent: template reasons. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5` | Model used for the above |
| `NEWS_API_KEY` | *(empty)* | Optional. NewsAPI headlines. Absent: Yahoo Finance headlines. |
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | Bind port |
| `API_RELOAD` | `0` | uvicorn auto-reload |
| `CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Comma-separated. Cannot be `*` — the API sends credentials. |
| `LOG_LEVEL` | `INFO` | Log level |
| `PORTFOLIO_FILE` | `backend/portfolio.json` | Where trade history is written |
| `PRICE_CACHE_TTL` | `900` | Price/OHLCV cache, seconds |
| `NEWS_CACHE_TTL` | `1800` | News cache, seconds |
| `CONTEXT_CACHE_TTL` | `21600` | Earnings/sector context cache, seconds |
| `REGIME_CACHE_TTL` | `900` | Market-regime cache, seconds |
| `SCAN_ON_STARTUP` | `1` | Run a full scan when the API boots |
| `SCAN_INTERVAL_MINUTES` | `30` | Scheduled scan cadence, weekdays 09-16 |
| `MOMENTUM_SCAN_LIMIT` | `120` | Tickers per momentum pass |
| `SCAN_MAX_WORKERS` | `12` | Scan thread-pool size |
| `STARTING_EQUITY` | `600` | Baseline for the equity curve |
| `BASE_RISK_PCT` | `0.02` | Risk per trade as a share of equity |
| `MIN_RISK_PCT` | `0.005` | Floor after throttling |
| `MAX_PORTFOLIO_HEAT` | `0.06` | Cap on total open risk |
| `MAX_OPEN_POSITIONS` | `4` | Concurrent position cap |
| `DD_HALVE_SIZE` | `0.10` | Drawdown off peak that halves size |
| `DD_STOP_TRADING` | `0.20` | Drawdown off peak that halts trading |
| `CONSECUTIVE_LOSS_PAUSE` | `4` | Losses in a row that force a pause |
| `TYPICAL_MIN_CONTRACT_COST` | `100` | Cheapest realistically liquid contract |
| `TAKE_PROFIT_PCT` | `0.70` | Share of max profit that triggers a take |
| `STOP_LOSS_PCT` | `0.40` | Share of premium lost that triggers a cut |
| `TIME_STOP_DTE` | `14` | Days-to-expiry time stop |
| `MAX_HOLD_DAYS` | `12` | Hard hold limit |
| `COMMISSION_PER_CONTRACT` | `0.65` | Backtest cost model, each way |
| `SLIPPAGE_FRAC_OF_PRICE` | `0.02` | Backtest slippage, each way |
| `NEXT_PUBLIC_API_URL` | *(empty)* | Frontend build-time API base. Empty = use the Next dev proxy. |

### Wiring `config.py` into the app

`config.py` is deliberately not yet imported by application code, so nothing has
changed behaviourally. To adopt it:

```python
# backend/main.py
from config import settings                       # replaces load_dotenv()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,          # was a hard-coded list
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

scheduler.add_job(_background_scan, "cron", day_of_week="mon-fri",
                  hour="9-16", minute=f"*/{settings.scan_interval_minutes}",
                  id="market_scan")

# lifespan: gate the startup scan
if settings.scan_on_startup:
    threading.Thread(target=_background_scan, daemon=True).start()

# /api/risk and /api/momentum defaults
def risk_status(starting_equity: float = settings.starting_equity): ...
def _momentum_scan(limit: int = settings.momentum_scan_limit): ...
```

Other modules, when their owners are ready:

- `backend/portfolio.py`: `_PORTFOLIO_FILE = str(settings.portfolio_file)`
- `backend/data/fetcher.py`: `CACHE_TTL = settings.price_cache_ttl`
- `backend/data/news.py`: `CACHE_TTL = settings.news_cache_ttl`, `NEWS_API_KEY = settings.news_api_key`
- `backend/signals/context.py`: `_TTL = settings.context_cache_ttl`
- `backend/risk.py`: `BASE_RISK_PCT = settings.base_risk_pct`, etc.
- `backend/ai/analyzer.py`: `Anthropic(api_key=settings.anthropic_api_key)`

`config.py` imports no application code, so importing it from any module is
cycle-free.

---

## Backtesting

### Walk-forward — the test that can kill the strategy

```bash
cd backend
python walkforward.py --years 3 --hold-days 5
python walkforward.py --years 3 --universe 250 --step 3 --picks 3 --split
python walkforward.py --exclude macd_rsi --split
```

| Flag | Default | Meaning |
|---|---|---|
| `--years` | `2` | History depth |
| `--hold-days` | `5` | Hold length |
| `--budget` | `600` | Capital per window |
| `--universe` | `200` | Tickers considered |
| `--step` | `5` | Trading days between entry dates |
| `--structure` | `single` | `single` long options or `spread` |
| `--picks` | `2` | Positions per direction per window |
| `--exclude` | — | Strategy keys to disable, e.g. `macd_rsi` |
| `--min-quality` | — | Quality tiers to accept |
| `--stop-pct` | — | Override the stop |
| `--split` | off | Report in-sample and out-of-sample halves separately |

Rolls entry dates forward with no look-ahead, replays the real profit ladder against
the actual price path, charges commission and slippage, and reports expectancy and
profit factor split by market regime. **Always use `--split`** when evaluating a
change: anything that improves in-sample but not out-of-sample is noise.

Re-run the pre-registered experiments with `backend/run_experiments.sh` (bash; on
Windows use Git Bash).

### Single-window backtest

```bat
run_backtest.bat --budget 1000 --hold-days 10
run_backtest.bat --as-of 2026-06-09
```

Answers "what would this week have done", not "does this work". Do not draw
conclusions from it.

---

## API endpoints

Base URL `http://localhost:8000`. Interactive docs at `/docs`.

### Scanning

| Method | Path | Description |
|---|---|---|
| GET | `/api/signals` | Latest scan results. Query: `signal=BUY\|SELL\|WATCH`, `limit` (20). Returns signals + macro + scan state. |
| GET | `/api/ticker/{ticker}` | On-demand scan of one ticker. 404 if no data. |
| POST | `/api/scan` | Trigger a full market scan in the background. |
| GET | `/api/status` | Last scan time, tickers scanned, signal count, macro score. |
| GET | `/api/momentum` | Short-term (1-3 day) momentum signals. Query: `limit` (20). |
| POST | `/api/momentum/scan` | Trigger a momentum scan in the background. |

### Options workflow

| Method | Path | Description |
|---|---|---|
| POST | `/api/plan` | Budget-allocated shopping list. Body: `budget` (600), `picks_per_side` (2), `structure` (`single`), `source` (`swing\|momentum\|both`). |
| POST | `/api/positions` | Record what you actually bought. Body: `{items: [...]}`. |
| GET | `/api/positions` | Open positions marked to market with HOLD/SCALE/SELL verdicts. |
| POST | `/api/positions/{id}/close` | Close a position. Body: `{exit_value}` (optional but supply it, or realized P&L stays null). |
| POST | `/api/positions/{id}/scale` | Sell part of a position. Body: `{contracts, exit_value}`. |
| PATCH | `/api/positions/{id}` | Correct a position to match the actual broker fill. Body: `{fields: {...}}`. |

### Risk, context and review

| Method | Path | Description |
|---|---|---|
| GET | `/api/risk` | Equity, drawdown, portfolio heat, circuit-breaker state, `undersized` flag. Query: `starting_equity` (600). |
| GET | `/api/regime` | Index trend — trading with or against the broad market. |
| GET | `/api/advice` | Which options structure suits current conditions. |
| GET | `/api/alerts` | Anything needing action now: SELL/SCALE verdicts + risk warnings. |
| GET | `/api/performance` | Realized track record + per-strategy attribution. |
| GET | `/api/review` | Recent closes with the conditions they were entered under. Query: `days` (7). |
| GET | `/health` | Liveness probe. |

---

## Data quality

Price and options data come from free Yahoo Finance endpoints and are delayed
roughly 15 minutes. This is adequate for multi-day swing decisions and inadequate
for intraday execution. Option premiums in backtests are modelled, not observed —
see point 5 above.

## Disclaimer

Personal, educational software. **Not financial advice.** No representation is made
that any strategy here is or will be profitable; the walk-forward evidence says the
baseline is not. Options can expire worthless and lose 100% of the premium paid.
You are responsible for your own trades.
