"""FastAPI backend - exposes scanner results as REST endpoints.

Security posture: this binds to 0.0.0.0 so the dashboard can reach it, which means
anything on the local network can too. The API can read a real trading history and
mutate positions, so it is not safe to leave completely open. Mitigations here:

  * Optional shared-secret auth (set API_TOKEN in .env to require it on writes).
  * Rate limiting, so a runaway frontend loop or a stray script cannot hammer the
    yfinance-backed endpoints into a rate-limit ban that blinds the whole app.
  * A global exception handler, so unexpected errors return a clean JSON error
    instead of leaking a stack trace containing file paths to the caller.
"""

import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import threading

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("trader.api")

# Optional shared secret. Unset (the default) keeps local single-user use frictionless;
# set it before exposing this beyond localhost.
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# Endpoints that trigger heavy work: a full ~900-ticker scan, or live option-chain
# fetches. Abusing these gets the upstream data source to rate-limit us.
_EXPENSIVE_PREFIXES = ("/api/scan", "/api/plan", "/api/momentum/scan", "/api/ticker")
RATE_LIMIT_WINDOW = 60          # seconds
RATE_LIMIT_DEFAULT = 120        # requests/min/client for cheap reads
RATE_LIMIT_EXPENSIVE = 10       # requests/min/client for the heavy ones
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _rate_limited(client: str, path: str) -> bool:
    cap = RATE_LIMIT_EXPENSIVE if path.startswith(_EXPENSIVE_PREFIXES) else RATE_LIMIT_DEFAULT
    now = time.time()
    key = f"{client}:{'exp' if cap == RATE_LIMIT_EXPENSIVE else 'std'}"
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= cap:
            return True
        q.append(now)
        return False

from scanner import run_scan, get_last_scan, scan_single
import portfolio

# Background scan state
_scan_lock = threading.Lock()
_scan_running = False


def _background_scan():
    global _scan_running
    with _scan_lock:
        if _scan_running:
            return
        _scan_running = True
    try:
        run_scan()
    finally:
        with _scan_lock:
            _scan_running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run initial scan on startup
    t = threading.Thread(target=_background_scan, daemon=True)
    t.start()

    # Schedule scans every 30 min during market hours
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _background_scan,
        "cron",
        day_of_week="mon-fri",
        hour="9-16",
        minute="*/30",
        id="market_scan",
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Trader API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Rate limiting + optional auth on state-changing calls."""
    path = request.url.path
    client = request.client.host if request.client else "unknown"

    if path.startswith("/api") and _rate_limited(client, path):
        log.warning("rate limit hit: %s %s", client, path)
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited",
                     "detail": "Too many requests. This protects the upstream market "
                               "data feed from being rate-limited, which would blind "
                               "the whole app."},
        )

    # Only writes are gated: reads stay open so the dashboard works without config.
    if API_TOKEN and request.method in ("POST", "PATCH", "DELETE") and path.startswith("/api"):
        if request.headers.get("X-API-Token", "") != API_TOKEN:
            log.warning("rejected unauthenticated write: %s %s", client, path)
            return JSONResponse(status_code=401,
                                content={"error": "unauthorized",
                                         "detail": "Missing or invalid X-API-Token."})

    started = time.time()
    response = await call_next(request)
    took = (time.time() - started) * 1000
    if took > 2000:
        log.info("slow request %s %s -> %.0fms", request.method, path, took)
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Return a clean error rather than leaking a stack trace with local file paths."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error",
                 "detail": "Something failed server-side. Check the backend console."},
    )


@app.get("/api/signals")
def get_signals(signal: str | None = None, limit: int = 20):
    """Return latest scan results. Filter by signal=BUY|SELL|WATCH."""
    data = get_last_scan()
    signals = data.get("signals", [])
    if signal:
        signals = [s for s in signals if s["signal"] == signal.upper()]
    return {
        "signals": signals[:limit],
        "macro": data.get("macro", {}),
        "scanned_at": data.get("scanned_at"),
        "total_scanned": data.get("total_scanned", 0),
        "scan_running": _scan_running,
    }


@app.get("/api/ticker/{ticker}")
def get_ticker(ticker: str):
    """On-demand scan for a specific ticker."""
    result = scan_single(ticker.upper())
    if result is None:
        raise HTTPException(status_code=404, detail=f"No data for {ticker.upper()}")
    return result


@app.post("/api/scan")
def trigger_scan(background_tasks: BackgroundTasks):
    """Manually trigger a full market scan."""
    if _scan_running:
        return {"status": "scan_already_running"}
    background_tasks.add_task(_background_scan)
    return {"status": "scan_started"}


@app.get("/api/status")
def status():
    data = get_last_scan()
    return {
        "scanned_at": data.get("scanned_at"),
        "total_scanned": data.get("total_scanned", 0),
        "signal_count": len(data.get("signals", [])),
        "scan_running": _scan_running,
        "macro_score": data.get("macro", {}).get("score", 0),
    }


# ---------- Guided options workflow ----------

class PlanRequest(BaseModel):
    budget: float = 600.0
    picks_per_side: int = 2
    structure: str = "single"
    source: str = "both"      # swing | momentum | both


def _momentum_as_signals() -> list[dict]:
    """Reshape momentum results into the signal shape build_plan expects.

    Without this the momentum scanner is a read-only curiosity: it finds strong
    short-term setups you then have no way to actually trade through the planner.
    """
    out = []
    for m in _momentum_cache.get("signals", []):
        conf = m["confidence"]
        out.append({
            "ticker": m["ticker"],
            "signal": m["signal"],
            "entry": m["entry"],
            "take_profit_1": m["target"],
            "stop_loss": m["stop_loss"],
            "confidence": conf,
            "quality": "high" if conf >= 70 else "medium" if conf >= 50 else "low",
            "strategy_breakdown": [{"name": "Short-Term Momentum",
                                    "score": m["score"], "reason": "; ".join(m["reasons"][:2])}],
            "triggers": m["reasons"][:2],
            "source": "momentum",
        })
    return out


class OpenRequest(BaseModel):
    items: list[dict]


@app.post("/api/plan")
def options_plan(req: PlanRequest):
    """After a scan, return a budget-allocated shopping list of options to buy."""
    signals: list[dict] = []
    if req.source in ("swing", "both"):
        for s in get_last_scan().get("signals", []):
            signals.append({**s, "source": "swing"})
    if req.source in ("momentum", "both"):
        have = {s["ticker"] for s in signals}
        # A ticker flagged by BOTH engines is confluence - keep the swing entry (it has
        # the full strategy breakdown) rather than duplicating the position.
        signals += [m for m in _momentum_as_signals() if m["ticker"] not in have]
    return portfolio.build_plan(req.budget, signals, req.picks_per_side,
                                structure=req.structure)


@app.post("/api/positions")
def add_positions(req: OpenRequest):
    """Record the spreads the user actually bought."""
    portfolio.open_positions(req.items)
    return {"status": "saved", "count": len(req.items)}


@app.get("/api/positions")
def list_positions():
    """Open positions marked to market with HOLD/SELL verdicts."""
    return {"positions": portfolio.evaluate()}


class CloseRequest(BaseModel):
    exit_value: float | None = None


class ScaleRequest(BaseModel):
    contracts: int
    exit_value: float | None = None


class UpdateRequest(BaseModel):
    fields: dict


@app.post("/api/positions/{pos_id}/close")
def close_pos(pos_id: str, req: CloseRequest | None = None):
    portfolio.close_position(pos_id, req.exit_value if req else None)
    return {"status": "closed", "id": pos_id}


@app.post("/api/positions/{pos_id}/scale")
def scale_pos(pos_id: str, req: ScaleRequest):
    """Sell part of a position (profit-ladder tier) and keep the rest open."""
    portfolio.scale_out(pos_id, req.contracts, req.exit_value)
    return {"status": "scaled", "id": pos_id, "contracts_sold": req.contracts}


@app.patch("/api/positions/{pos_id}")
def edit_pos(pos_id: str, req: UpdateRequest):
    """Correct a position to match what actually filled in your broker."""
    portfolio.update_position(pos_id, req.fields)
    return {"status": "updated", "id": pos_id}


@app.get("/api/performance")
def performance():
    """Realized track record + per-strategy attribution."""
    return portfolio.performance_stats()


@app.get("/api/regime")
def regime():
    """Index trend - are we trading with or against the broad market?"""
    from signals.context import market_regime
    return market_regime()


@app.get("/api/risk")
def risk_status(starting_equity: float = 600.0):
    """Account-level risk: equity, drawdown, portfolio heat, circuit-breaker state."""
    import risk as risk_mod
    return risk_mod.to_dict(risk_mod.assess(portfolio._load(), starting_equity))


@app.get("/api/advice")
def advice():
    """Which options structure suits today's market."""
    from signals.context import recommended_structure
    return recommended_structure()


@app.get("/api/alerts")
def alerts():
    """Anything needing action right now, so you don't have to open the app to find out."""
    import risk as risk_mod
    out = []
    for p in portfolio.evaluate():
        if p["action"] in ("SELL", "SCALE"):
            out.append({
                "level": "action",
                "ticker": p["ticker"],
                "title": (f"SELL all {p['contracts']}" if p["action"] == "SELL"
                          else f"SELL {p['sell_contracts']} of {p['contracts']}"),
                "detail": p["reason"],
                "pnl": p["pnl"], "pnl_pct": p["pnl_pct"],
            })
    r = risk_mod.assess(portfolio._load())
    if r.status != "normal":
        out.append({"level": "risk", "ticker": None,
                    "title": f"Risk status: {r.status}",
                    "detail": " ".join(r.messages), "pnl": None, "pnl_pct": None})
    return {"alerts": out, "count": len(out)}


@app.get("/api/review")
def weekly_review(days: int = 7):
    """What closed recently, and what the record says about it.

    Most improvement comes from reviewing your own trades, not from better signals.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    closed = []
    for p in portfolio._load():
        if p.get("status") != "closed" or not p.get("closed_at"):
            continue
        try:
            when = datetime.fromisoformat(p["closed_at"])
        except Exception:
            continue
        if when < cutoff:
            continue
        ctx = p.get("entry_context") or {}
        closed.append({
            "ticker": p["ticker"], "kind": p.get("kind"),
            "closed_at": p["closed_at"],
            "realized_pnl": p.get("realized_pnl"),
            "strategies": ctx.get("strategies", []),
            "quality_score": ctx.get("quality_score"),
            "iv_verdict": ctx.get("iv_verdict"),
            "warnings_at_entry": ctx.get("warnings", []),
        })
    tracked = [c for c in closed if c["realized_pnl"] is not None]
    total = round(sum(c["realized_pnl"] for c in tracked), 2)
    wins = len([c for c in tracked if c["realized_pnl"] > 0])
    return {
        "days": days,
        "closed": closed,
        "closed_count": len(closed),
        "tracked_count": len(tracked),
        "realized_pnl": total,
        "wins": wins, "losses": len(tracked) - wins,
        "prompt": ("For each loss: was it the signal, the entry price, the exit timing, "
                   "or just the market? Only the first two are things you control."),
        "performance": portfolio.performance_stats(),
    }


# ---------- Short-term momentum (1-3 day holds) ----------

_momentum_cache: dict = {"signals": [], "scanned_at": None, "running": False}


def _momentum_scan(limit: int = 120):
    import concurrent.futures, time as _t
    from signals.intraday import score_momentum, signal_to_dict
    from data.universe import get_universe

    _momentum_cache["running"] = True
    try:
        # Rank by the daily scan's conviction first so the intraday pass spends its
        # (much slower, one-download-per-ticker) budget on names already worth watching.
        daily = get_last_scan().get("signals", [])
        preferred = [s["ticker"] for s in daily]
        universe = preferred + [t for t in get_universe() if t not in set(preferred)]
        tickers = universe[:limit]

        out = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            for m in ex.map(lambda t: score_momentum(t), tickers):
                if m and m.signal != "WATCH":
                    out.append(signal_to_dict(m))
        out.sort(key=lambda d: -abs(d["score"]))
        _momentum_cache["signals"] = out
        _momentum_cache["scanned_at"] = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    except Exception as e:
        print(f"[momentum] scan error: {e}")
    finally:
        _momentum_cache["running"] = False


@app.get("/api/momentum")
def momentum(limit: int = 20):
    return {
        "signals": _momentum_cache["signals"][:limit],
        "scanned_at": _momentum_cache["scanned_at"],
        "running": _momentum_cache["running"],
        "note": ("Short-term momentum from the last completed session, for 1-3 day holds. "
                 "Not intraday scalping: the free data feed is ~15 min delayed, and a US "
                 "account under $25k is capped at 3 day trades per rolling 5 business days "
                 "(PDT rule)."),
    }


@app.post("/api/momentum/scan")
def trigger_momentum(background_tasks: BackgroundTasks):
    if _momentum_cache["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(_momentum_scan)
    return {"status": "started"}


@app.get("/health")
def health():
    """Liveness + readiness. A monitor needs to distinguish 'process is up' from
    'process is up but has never successfully scanned', which are very different."""
    data = get_last_scan()
    scanned = data.get("total_scanned", 0)
    stale = None
    if data.get("scanned_at"):
        from datetime import datetime, timezone
        try:
            when = datetime.strptime(data["scanned_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            stale = round((datetime.now(timezone.utc) - when).total_seconds() / 60, 1)
        except Exception:
            pass

    ready = scanned > 0
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "starting",
            "ready": ready,
            "scanned_tickers": scanned,
            "scan_running": _scan_running,
            "minutes_since_scan": stale,
            "auth_required_on_writes": bool(API_TOKEN),
        },
    )
