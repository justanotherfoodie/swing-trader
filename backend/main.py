"""FastAPI backend — exposes scanner results as REST endpoints."""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import threading

load_dotenv()

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
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {"status": "ok"}
