"""
Guided options workflow: budget -> what to buy -> track -> when to sell.

Three jobs:
  1. build_plan(budget): after a scan, allocate the budget across the best call/put
     spreads and return a concrete shopping list (how many of each contract to buy).
     Prices are re-fetched LIVE at request time (not reused from the last market scan)
     so the quote you see is as fresh as the moment you click, not hours-old evening
     data from before an overnight gap. Wide-bid/ask (illiquid) spreads are deprioritized
     since their quoted price won't hold at fill time.
  2. open_positions(items): persist what the user actually bought (portfolio.json).
  3. evaluate(): for every open position, mark it to current market and return a plain
     HOLD or SELL verdict with a reason and timing ("sell at next open").

Exit rules (the "when to sell" brain), checked in priority order:
  STOP        - underlying broke the stop level, OR the spread lost >=50% of premium
  BIG WIN     - spread gained >=75% on premium -> take the win outright (rare for a
                debit spread to run much further; don't get greedy)
  PROFIT LOCK - position peaked at >=30% gain and has since given back >=12 points of
                that gain -> sell before it round-trips to breakeven. This is the fix
                for "no threshold when profit reaches a desired %": previously the ONLY
                profit exit required the stock to hit its exact target price or the
                spread to reach ~max profit (both rare mid-trade), so a position could
                spike +50% intraday and drift back to flat with no SELL ever firing.
  TARGET      - underlying reached the profit target, OR spread captured >=70% of max
  TIME        - <=14 days to expiry (theta/gamma cliff) OR held >=12 calendar days
  FLIP        - the stock's signal flipped against the position (thesis broken)
  else        - HOLD, with progress toward target
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone

import numpy as np
import yfinance as yf

from data.fetcher import get_ohlcv, get_current_price
from signals.scorer import score_ticker
from signals.options import black_scholes, build_options_play, play_to_dict

_PORTFOLIO_FILE = os.path.join(os.path.dirname(__file__), "portfolio.json")
_lock = threading.Lock()

TAKE_PROFIT_PCT = 0.70    # captured this share of max profit -> take it
STOP_LOSS_PCT = 0.50      # lost this share of premium -> cut it
TIME_STOP_DTE = 14
MAX_HOLD_DAYS = 12
BIG_WIN_PCT = 75           # premium gain this large -> take it outright
PROFIT_LOCK_TRIGGER_PCT = 30   # peak gain needed before we start protecting it
PROFIT_LOCK_GIVEBACK_PCT = 12  # percentage-point pullback from peak that triggers sell


# ---------- persistence ----------

def _load() -> list[dict]:
    if not os.path.exists(_PORTFOLIO_FILE):
        return []
    try:
        with open(_PORTFOLIO_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(positions: list[dict]):
    with open(_PORTFOLIO_FILE, "w") as f:
        json.dump(positions, f, indent=2)


# ---------- plan building ----------

def build_plan(budget: float, signals: list[dict], picks_per_side: int = 2,
               candidate_pool: int = 6) -> dict:
    """Allocate budget across the best spreads, re-priced LIVE at request time.

    The signal list (direction, quality, confidence) comes from the last market scan -
    that's fine, it's based on completed daily candles and doesn't need to be live.
    But the OPTION PRICE does need to be live: reusing the scan-time premium means a
    plan built in the evening quotes hours-stale prices by the time you actually buy
    the next morning. So here we take the top `candidate_pool` signals per side and
    re-fetch a fresh option chain quote for each one right now.
    """
    have_opts = [s for s in signals if s.get("options_play")]
    buys  = [s for s in have_opts if s["signal"] == "BUY"]
    sells = [s for s in have_opts if s["signal"] == "SELL"]

    qrank = {"high": 0, "medium": 1, "low": 2}
    buys.sort(key=lambda s: (qrank.get(s["quality"], 3), -s["confidence"]))
    sells.sort(key=lambda s: (qrank.get(s["quality"], 3), -s["confidence"]))

    if not buys and not sells:
        return {"budget": budget, "items": [], "total_cost": 0,
                "cash_left": budget, "note": "No options-eligible signals in the latest scan.",
                "priced_at": datetime.now(timezone.utc).isoformat()}

    def make_draft(s):
        """Re-quote this signal's spread live (fresh bid/ask), not the scan-time cache."""
        fresh = build_options_play(s["ticker"], s["signal"], s["entry"],
                                   s["take_profit_1"], s["stop_loss"])
        op = play_to_dict(fresh)
        if op is None:
            return None
        per_contract = op["net_debit"] * 100
        if per_contract <= 0:
            return None
        return {
            "ticker": s["ticker"],
            "signal": s["signal"],
            "kind": "call" if s["signal"] == "BUY" else "put",
            "strategy": op["strategy"],
            "expiry": op["expiry"],
            "long_strike": op["legs"][0]["strike"],
            "short_strike": op["legs"][1]["strike"],
            "net_debit": op["net_debit"],
            "per_contract": round(per_contract, 2),
            "max_profit": op["max_profit"],
            "breakeven": op["breakeven"],
            "risk_reward": op["risk_reward"],
            "prob_profit": op["prob_profit"],
            "entry_spot": s["entry"],
            "target": s["take_profit_1"],
            "stop": s["stop_loss"],
            "quality": s["quality"],
            "confidence": s["confidence"],
            "wide_market": op.get("wide_market", False),
            "max_spread_pct": op.get("max_spread_pct", 0),
            "contracts": 0,
        }

    # Re-quote only the top few candidates per side (live chain calls are not free) -
    # quality-ranked already, so this stays the same "best signals" set as before.
    buy_drafts = [d for d in (make_draft(s) for s in buys[:candidate_pool]) if d]
    sell_drafts = [d for d in (make_draft(s) for s in sells[:candidate_pool]) if d]

    # Prefer liquid (tight bid/ask) picks - a wide-market quote won't hold at fill time,
    # so push those to the back instead of excluding them outright (still tradeable,
    # just riskier to price precisely).
    buy_drafts.sort(key=lambda d: d["wide_market"])
    sell_drafts.sort(key=lambda d: d["wide_market"])

    # Seed phase: alternate sides, each time taking the best UNUSED, AFFORDABLE pick.
    # Scanning deeper finds a cheap-enough put even when the top picks are pricey
    # large-caps, so a small budget still gets a real call+put hedge.
    chosen = []
    cash = 0.0
    taken = set()

    def try_seed(pool):
        nonlocal cash
        for d in pool:
            if (d["contracts"] == 0 and d["ticker"] not in taken
                    and cash + d["per_contract"] <= budget):
                d["contracts"] = 1
                cash += d["per_contract"]
                taken.add(d["ticker"])
                chosen.append(d)
                return True
        return False

    for _ in range(picks_per_side):
        try_seed(buy_drafts)
        try_seed(sell_drafts)

    # Round-robin top-up: spend the rest evenly across chosen positions.
    improved = True
    while improved:
        improved = False
        for d in chosen:
            if cash + d["per_contract"] <= budget:
                d["contracts"] += 1
                cash += d["per_contract"]
                improved = True

    items = [d for d in chosen if d["contracts"] > 0]
    for d in items:
        d["cost"] = round(d["per_contract"] * d["contracts"], 2)
        d["max_gain_total"] = round(d["max_profit"] * 100 * d["contracts"], 2)
        d["max_loss_total"] = d["cost"]

    n_calls = sum(d["contracts"] for d in items if d["kind"] == "call")
    n_puts = sum(d["contracts"] for d in items if d["kind"] == "put")
    wide_tickers = [d["ticker"] for d in items if d.get("wide_market")]
    note = (f"Buy {n_calls} call-spread + {n_puts} put-spread contracts. "
            f"Defined risk: most you can lose is ${cash:,.0f}.")
    if wide_tickers:
        note += (f" Note: {', '.join(wide_tickers)} has a wide bid/ask spread - "
                 "use a limit order, your fill may differ from the quote below.")

    return {
        "budget": budget,
        "items": items,
        "total_cost": round(cash, 2),
        "cash_left": round(budget - cash, 2),
        "n_call_contracts": n_calls,
        "n_put_contracts": n_puts,
        "note": note,
        "priced_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- opening positions ----------

def open_positions(items: list[dict]) -> list[dict]:
    """Persist the spreads the user actually bought."""
    now = datetime.now(timezone.utc).isoformat()
    positions = _load()
    with _lock:
        for it in items:
            positions.append({
                "id": uuid.uuid4().hex[:8],
                "ticker": it["ticker"],
                "kind": it["kind"],
                "strategy": it.get("strategy", ""),
                "expiry": it["expiry"],
                "long_strike": it["long_strike"],
                "short_strike": it["short_strike"],
                "net_debit": it["net_debit"],
                "contracts": it["contracts"],
                "entry_spot": it["entry_spot"],
                "target": it["target"],
                "stop": it["stop"],
                "opened_at": now,
                "status": "open",
                "peak_pnl_pct": 0.0,   # tracks the best gain seen so far, for profit-lock
            })
        _save(positions)
    return positions


def close_position(pos_id: str):
    positions = _load()
    with _lock:
        for p in positions:
            if p["id"] == pos_id:
                p["status"] = "closed"
                p["closed_at"] = datetime.now(timezone.utc).isoformat()
        _save(positions)
    return _load()


# ---------- valuation + exit brain ----------

def _realized_iv(ticker: str) -> float:
    df = get_ohlcv(ticker, period="3mo")
    if df is None or df.empty:
        return 0.30
    rets = np.log(df["close"] / df["close"].shift(1)).dropna().tail(20)
    if len(rets) < 5:
        return 0.30
    return max(0.10, min(2.0, float(rets.std() * (252 ** 0.5))))


def _spread_mark(ticker, expiry, long_k, short_k, kind, spot, dte) -> float:
    """Current per-share value of the spread. Live chain mid if available, else BS."""
    try:
        t = yf.Ticker(ticker)
        chain = t.option_chain(expiry)
        df = chain.calls if kind == "call" else chain.puts

        def mid(strike):
            row = df[df["strike"] == strike]
            if row.empty:
                return None
            b = float(row.iloc[0].get("bid") or 0)
            a = float(row.iloc[0].get("ask") or 0)
            last = float(row.iloc[0].get("lastPrice") or 0)
            if b > 0 and a > 0:
                return (b + a) / 2
            return last if last > 0 else None

        lm, sm = mid(long_k), mid(short_k)
        if lm is not None and sm is not None:
            return max(0.0, lm - sm)
    except Exception:
        pass
    # Fallback: Black-Scholes
    iv = _realized_iv(ticker)
    return max(0.0, black_scholes(spot, long_k, dte, iv, kind)
               - black_scholes(spot, short_k, dte, iv, kind))


def _evaluate_one(p: dict) -> dict:
    spot = get_current_price(p["ticker"]) or p["entry_spot"]
    today = datetime.now(timezone.utc).date()
    exp = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
    dte = (exp - today).days
    opened = datetime.fromisoformat(p["opened_at"]).date()
    days_held = (today - opened).days

    width = abs(p["short_strike"] - p["long_strike"])
    entry_debit = p["net_debit"]
    max_profit = max(0.01, width - entry_debit)

    mark = _spread_mark(p["ticker"], p["expiry"], p["long_strike"], p["short_strike"],
                        p["kind"], spot, dte)
    contracts = p["contracts"]
    cost = round(entry_debit * 100 * contracts, 2)
    value = round(mark * 100 * contracts, 2)
    pnl = round(value - cost, 2)
    pnl_pct = round(pnl / cost * 100, 1) if cost else 0
    pct_of_max = (mark - entry_debit) / max_profit   # 1.0 = full max profit

    # Track the best gain this position has ever shown, so we can detect "spiked then
    # gave it back" even when the stock never quite reached its exact target price.
    peak_pnl_pct = max(float(p.get("peak_pnl_pct", 0.0)), pnl_pct)

    # ---- exit rules, priority order ----
    action, reason, urgency = "HOLD", "", "watch"
    is_call = p["kind"] == "call"

    broke_stop = (spot <= p["stop"]) if is_call else (spot >= p["stop"])
    hit_target = (spot >= p["target"]) if is_call else (spot <= p["target"])

    # direction flip
    flipped = False
    try:
        df = get_ohlcv(p["ticker"], period="6mo")
        sig = score_ticker(p["ticker"], df, 0.0) if df is not None and not df.empty else None
        if sig:
            if is_call and sig.signal == "SELL":
                flipped = True
            if (not is_call) and sig.signal == "BUY":
                flipped = True
    except Exception:
        pass

    gave_back = (peak_pnl_pct >= PROFIT_LOCK_TRIGGER_PCT
                and pnl_pct <= peak_pnl_pct - PROFIT_LOCK_GIVEBACK_PCT)

    if broke_stop or pnl_pct <= -STOP_LOSS_PCT * 100:
        action, urgency = "SELL", "now"
        reason = ("Underlying broke your stop level. " if broke_stop else
                  f"Spread down {pnl_pct:.0f}%. ") + "Cut the loss — sell at next market open."
    elif pnl_pct >= BIG_WIN_PCT:
        action, urgency = "SELL", "now"
        reason = (f"Up {pnl_pct:.0f}% on premium — take the win. Getting much further "
                  "requires the stock to run all the way to your short strike, which is "
                  "the low-probability tail case. Lock it in at next market open.")
    elif gave_back:
        action, urgency = "SELL", "now"
        reason = (f"Peaked at +{peak_pnl_pct:.0f}% and has slipped to +{pnl_pct:.0f}% — "
                  "lock in the gain before it round-trips to breakeven. Sell at next open.")
    elif hit_target or pct_of_max >= TAKE_PROFIT_PCT:
        action, urgency = "SELL", "now"
        reason = (f"Target reached — captured ~{max(0,pct_of_max)*100:.0f}% of max profit. "
                  "Lock it in: sell at next market open.")
    elif dte <= TIME_STOP_DTE:
        action, urgency = "SELL", "now"
        reason = (f"Only {dte} days to expiry — time decay accelerates from here. "
                  "Close it at next market open regardless of P&L.")
    elif days_held >= MAX_HOLD_DAYS:
        action, urgency = "SELL", "now"
        reason = (f"Held {days_held} days — past the swing window. "
                  "Take what's there and move on.")
    elif flipped:
        action, urgency = "SELL", "now"
        reason = "The stock's signal flipped against you — thesis broken. Exit at next open."
    else:
        progress = pct_of_max * 100
        if peak_pnl_pct >= 15 and pnl_pct < peak_pnl_pct:
            reason = (f"Hold, but watch it — peaked at +{peak_pnl_pct:.0f}%, now +{pnl_pct:.0f}%. "
                      f"Will flag SELL if it slips to +{peak_pnl_pct - PROFIT_LOCK_GIVEBACK_PCT:.0f}%.")
        elif progress < 5:
            reason = (f"Hold. Still early ({days_held}d held, {dte}d to expiry) — "
                      "a small day-1 dip is just the bid/ask spread, not a loss yet. "
                      "Let the trade work.")
        else:
            reason = (f"On track — hold. ~{progress:.0f}% of the way to max profit, "
                      f"{days_held}d held, {dte}d to expiry.")

    return {
        **{k: p[k] for k in ("id", "ticker", "kind", "strategy", "expiry",
                             "long_strike", "short_strike", "contracts",
                             "entry_spot", "target", "stop", "opened_at")},
        "net_debit": entry_debit,
        "spot_now": round(spot, 2),
        "days_held": days_held,
        "dte": dte,
        "cost": cost,
        "value": value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "peak_pnl_pct": round(peak_pnl_pct, 1),
        "pct_of_max": round(max(0.0, pct_of_max) * 100),
        "action": action,
        "urgency": urgency,
        "reason": reason,
    }


def evaluate() -> list[dict]:
    """Return current HOLD/SELL verdicts for all open positions.

    Also persists the updated peak_pnl_pct back to disk on every call, so the
    profit-lock high-water mark survives across restarts and repeated checks.
    """
    all_positions = _load()
    out = []
    dirty = False
    for p in all_positions:
        if p.get("status") != "open":
            continue
        try:
            result = _evaluate_one(p)
            out.append(result)
            if result["peak_pnl_pct"] != p.get("peak_pnl_pct", 0.0):
                p["peak_pnl_pct"] = result["peak_pnl_pct"]
                dirty = True
        except Exception as e:
            print(f"[portfolio] eval error {p.get('ticker')}: {e}")
    if dirty:
        with _lock:
            _save(all_positions)
    return out
