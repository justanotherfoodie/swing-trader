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
from signals.options import (black_scholes, build_options_play, build_long_option,
                             build_credit_spread, play_to_dict)

_PORTFOLIO_FILE = os.path.join(os.path.dirname(__file__), "portfolio.json")
_lock = threading.Lock()

TAKE_PROFIT_PCT = 0.70    # captured this share of max profit -> take it
STOP_LOSS_PCT = 0.40      # lost this share of premium -> cut it (was 0.50; long options
                          # decay faster than spreads, so cut sooner)
TIME_STOP_DTE = 14
MAX_HOLD_DAYS = 12

# ---- Profit ladder ----------------------------------------------------------
# Scale OUT in tiers instead of holding for a home run. A long option that is up 30%
# can be flat two sessions later; taking a chunk off converts paper gains into money
# while leaving a runner for the move to continue. These are the numbers that answer
# "why not take profit at 20 or 30%".
TIER1_PCT = 25            # first scale-out: sell ~half
TIER2_PCT = 50            # second scale-out: sell ~half of what's left
TIER3_PCT = 80            # close the runner entirely
SINGLE_CT_TAKE_PCT = 30   # can't split 1 contract - just take the whole thing here
TRAIL_ARM_PCT = 20        # once peaked at least this high, protect it
# Giveback scales with the peak instead of being a flat 8 points. A fixed stop is
# proportionally brutal on a small peak (21% -> out at 13%, which is ordinary noise)
# and far too loose on a big one (80% -> out at 72%). Backtesting the flat version
# showed it cutting a runner that went on to triple. Keep ~a third of the peak.
TRAIL_GIVEBACK_FRAC = 0.35
TRAIL_GIVEBACK_MIN = 10


def trail_exit_level(peak_pct: float) -> float:
    """The P&L level at which a trailing exit fires, given the best gain seen."""
    return peak_pct - max(TRAIL_GIVEBACK_MIN, peak_pct * TRAIL_GIVEBACK_FRAC)

# No single ticker may absorb more than this share of the budget. Concentration is the
# fastest way to lose a small account: one gap-down on your only position is the account.
MAX_POSITION_PCT = 0.40

# Trade-quality floor. Plays scoring below this are excluded outright: they are the
# expensive-premium / earnings-landmine / no-liquidity setups that lose money slowly
# even when the direction is right. Between the floor and "good" they are allowed but
# ranked behind clean setups.
MIN_QUALITY = 50
GOOD_QUALITY = 75

# Two calls in the same sector is one bet wearing two hats - XOM and CVX do not diversify
# each other. One position per sector keeps the portfolio genuinely spread.
MAX_PER_SECTOR = 1


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
               candidate_pool: int = 6, structure: str = "single") -> dict:
    """Allocate budget across the best spreads, re-priced LIVE at request time.

    The signal list (direction, quality, confidence) comes from the last market scan -
    that's fine, it's based on completed daily candles and doesn't need to be live.
    But the OPTION PRICE does need to be live: reusing the scan-time premium means a
    plan built in the evening quotes hours-stale prices by the time you actually buy
    the next morning. So here we take the top `candidate_pool` signals per side and
    re-fetch a fresh option chain quote for each one right now.
    """
    # Signals that already carry a scan-time options quote are known to be optionable.
    # Momentum signals arrive without one; make_draft re-quotes live anyway, so let it
    # decide eligibility rather than excluding them up front.
    have_opts = [s for s in signals
                 if s.get("options_play") or s.get("source") == "momentum"]
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
        """Re-quote this signal live (fresh bid/ask), not the scan-time cache.

        `structure` picks what to actually buy: "single" = one long call/put (two taps
        in any broker, uncapped upside, whole premium at risk) or "spread" = a vertical
        debit spread (cheaper, capped both ways, multi-leg order).
        """
        builder = {"single": build_long_option,
                   "spread": build_options_play,
                   "credit": build_credit_spread}.get(structure, build_long_option)
        fresh = builder(s["ticker"], s["signal"], s["entry"],
                        s["take_profit_1"], s["stop_loss"])
        op = play_to_dict(fresh)
        if op is None:
            return None
        # For a credit spread the capital committed is the max LOSS (collateral held),
        # not a debit paid - net_debit is negative because money comes in.
        per_contract = (op["max_loss"] * 100 if structure == "credit"
                        else op["net_debit"] * 100)
        if per_contract <= 0:
            return None
        legs = op["legs"]
        return {
            "ticker": s["ticker"],
            "signal": s["signal"],
            "kind": "call" if s["signal"] == "BUY" else "put",
            "strategy": op["strategy"],
            "expiry": op["expiry"],
            "long_strike": legs[0]["strike"],
            "short_strike": legs[1]["strike"] if len(legs) > 1 else None,
            "structure": "single" if len(legs) == 1 else "spread",
            "net_debit": op["net_debit"],
            "per_contract": round(per_contract, 2),
            "max_profit": op["max_profit"],
            "max_loss": op["max_loss"],
            "limit_guidance": op.get("limit_guidance"),
            "breakeven": op["breakeven"],
            "risk_reward": op["risk_reward"],
            "prob_profit": op["prob_profit"],
            "entry_spot": s["entry"],
            "target": s["take_profit_1"],
            "stop": s["stop_loss"],
            "quality": s["quality"],
            "confidence": s["confidence"],
            # Which of the 5 strategies actually fired - the key the journal groups by.
            "strategies": [b["name"] for b in s.get("strategy_breakdown", [])
                           if b.get("score")] or ([s["source"]] if s.get("source") else []),
            "triggers": s.get("triggers", []),
            "source": s.get("source", "swing"),
            "wide_market": op.get("wide_market", False),
            "max_spread_pct": op.get("max_spread_pct", 0),
            "quality_score": op.get("quality_score", 100),
            "warnings": op.get("warnings", []),
            "delta": op.get("delta", 0),
            "open_interest": op.get("open_interest", 0),
            "iv_verdict": op.get("iv_verdict", ""),
            "iv_ratio": op.get("iv_ratio"),
            "earnings_date": op.get("earnings_date"),
            "earnings_in_hold": op.get("earnings_in_hold", False),
            "against_trend": op.get("against_trend", False),
            "contracts": 0,
        }

    # Re-quote only the top few candidates per side (live chain calls are not free) -
    # quality-ranked already, so this stays the same "best signals" set as before.
    buy_drafts = [d for d in (make_draft(s) for s in buys[:candidate_pool]) if d]
    sell_drafts = [d for d in (make_draft(s) for s in sells[:candidate_pool]) if d]

    # Drop the trades that lose money quietly regardless of direction: expensive premium,
    # earnings landmines, no liquidity. Then rank what's left by quality first - a clean
    # 75-quality setup beats a flashy signal wrapped in a bad contract.
    rejected = [d for d in buy_drafts + sell_drafts if d["quality_score"] < MIN_QUALITY]
    buy_drafts = [d for d in buy_drafts if d["quality_score"] >= MIN_QUALITY]
    sell_drafts = [d for d in sell_drafts if d["quality_score"] >= MIN_QUALITY]
    buy_drafts.sort(key=lambda d: (-d["quality_score"], d["wide_market"]))
    sell_drafts.sort(key=lambda d: (-d["quality_score"], d["wide_market"]))

    # Seed phase: alternate sides, each time taking the best UNUSED, AFFORDABLE pick.
    # Scanning deeper finds a cheap-enough put even when the top picks are pricey
    # large-caps, so a small budget still gets a real call+put hedge.
    chosen = []
    cash = 0.0
    taken = set()
    sector_count: dict[str, int] = {}

    def try_seed(pool):
        nonlocal cash
        from signals.context import get_sector
        for d in pool:
            if d["contracts"] != 0 or d["ticker"] in taken:
                continue
            if cash + d["per_contract"] > budget:
                continue
            sec = get_sector(d["ticker"])
            d["sector"] = sec
            # One position per sector: two energy calls is a single bet on oil.
            if sec != "Unknown" and sector_count.get(sec, 0) >= MAX_PER_SECTOR:
                continue
            d["contracts"] = 1
            cash += d["per_contract"]
            taken.add(d["ticker"])
            sector_count[sec] = sector_count.get(sec, 0) + 1
            chosen.append(d)
            return True
        return False

    for _ in range(picks_per_side):
        try_seed(buy_drafts)
        try_seed(sell_drafts)

    # Round-robin top-up, but capped per position. Without a cap the cheapest contract
    # absorbs the entire budget - six contracts of one ticker is a single undiversified
    # bet, not a portfolio, and one bad earnings print takes the whole account with it.
    max_per_position = budget * MAX_POSITION_PCT
    improved = True
    while improved:
        improved = False
        for d in chosen:
            spent_here = d["per_contract"] * (d["contracts"] + 1)
            if cash + d["per_contract"] <= budget and spent_here <= max_per_position:
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

    if structure == "credit":
        note = (f"Sell {len(items)} credit spread position{'s' if len(items) != 1 else ''} "
                f"({n_calls + n_puts} contracts). ${cash:,.0f} held as collateral against "
                f"the maximum loss; the credit received is yours to keep if the stocks "
                f"stay put.")
    else:
        label = "call" if structure == "single" else "call-spread"
        plabel = "put" if structure == "single" else "put-spread"
        note = (f"Buy {n_calls} {label} + {n_puts} {plabel} contracts across "
                f"{len(items)} position{'s' if len(items) != 1 else ''}. "
                f"Most you can lose is ${cash:,.0f} (the full premium).")

    # Explain an empty plan caused by affordability rather than signal quality - an
    # unexplained blank list looks like a broken scanner.
    if not items and (buy_drafts or sell_drafts):
        cheapest = min((d["per_contract"] for d in buy_drafts + sell_drafts), default=0)
        cap = budget * MAX_POSITION_PCT
        note = (f"Signals passed the quality filters, but nothing fits this budget. "
                f"The cheapest qualifying position needs ${cheapest:,.0f} per contract, "
                f"and the per-position cap at a ${budget:,.0f} budget is ${cap:,.0f} "
                f"(40%). "
                + ("Credit spreads tie up collateral equal to their max loss, which is "
                   "typically several hundred dollars per contract - they need a larger "
                   "account than buying a single option does."
                   if structure == "credit" else
                   "Try a larger budget, or a cheaper structure."))
    if wide_tickers:
        note += (f" Note: {', '.join(wide_tickers)} has a wide bid/ask spread - "
                 "use a limit order, your fill may differ from the quote below.")

    from signals.context import (market_regime, premium_buying_environment,
                                 recommended_structure)
    import risk as risk_mod
    risk_state = risk_mod.to_dict(risk_mod.assess(_load(), starting_equity=budget))
    return {
        "budget": budget,
        "items": items,
        "structure": structure,
        "environment": premium_buying_environment(),
        "advice": recommended_structure(),
        "risk": risk_state,
        "total_cost": round(cash, 2),
        "cash_left": round(budget - cash, 2),
        "n_call_contracts": n_calls,
        "n_put_contracts": n_puts,
        "note": note,
        "priced_at": datetime.now(timezone.utc).isoformat(),
        "regime": market_regime(),
        # Surfaced so a quiet plan is explained rather than mysterious: these are the
        # signals that were filtered out for reasons unrelated to direction.
        "rejected": [{"ticker": d["ticker"], "quality_score": d["quality_score"],
                      "warnings": d["warnings"]} for d in rejected][:8],
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
                "short_strike": it.get("short_strike"),
                "net_debit": it["net_debit"],
                "contracts": it["contracts"],
                "entry_spot": it["entry_spot"],
                "target": it["target"],
                "stop": it["stop"],
                "opened_at": now,
                "status": "open",
                "peak_pnl_pct": 0.0,   # tracks the best gain seen so far, for profit-lock
                "scaled_out": [],      # which profit-ladder rungs have been taken
                # ---- journal: the conditions at entry, so outcomes can be attributed ----
                # Without this the app can never learn which strategy actually pays. Every
                # closed trade with these fields is one data point toward killing the
                # losing setups and sizing up the winners.
                "entry_context": {
                    "strategies": it.get("strategies", []),
                    "quality_score": it.get("quality_score"),
                    "iv_verdict": it.get("iv_verdict"),
                    "iv_ratio": it.get("iv_ratio"),
                    "delta": it.get("delta"),
                    "open_interest": it.get("open_interest"),
                    "confidence": it.get("confidence"),
                    "signal_quality": it.get("quality"),
                    "sector": it.get("sector"),
                    "regime": it.get("regime"),
                    "warnings": it.get("warnings", []),
                },
            })
        _save(positions)
    return positions


def close_position(pos_id: str, exit_value: float | None = None):
    """Close a position fully. `exit_value` is total $ received, for realized-P&L stats."""
    positions = _load()
    with _lock:
        for p in positions:
            if p["id"] == pos_id:
                p["status"] = "closed"
                p["closed_at"] = datetime.now(timezone.utc).isoformat()
                if exit_value is not None:
                    p["exit_value"] = round(float(exit_value), 2)
                    # Cost basis of the contracts STILL open - earlier scale-outs already
                    # accounted for theirs. Accumulate rather than overwrite, or banking a
                    # partial profit silently erases it from the record the moment the
                    # remainder is closed.
                    cost = p["net_debit"] * 100 * p["contracts"]
                    leg_pnl = round(float(exit_value) - cost, 2)
                    p["realized_pnl"] = round(p.get("realized_pnl", 0.0) + leg_pnl, 2)
        _save(positions)
    return _load()


def scale_out(pos_id: str, contracts_sold: int, exit_value: float | None = None):
    """Sell PART of a position and keep the rest open.

    This is what makes the profit ladder real: banking half at +25% is only possible if
    the tracker can hold a partially-closed position. Records the realized piece and
    marks which ladder rung was taken so the same tier doesn't fire again.
    """
    positions = _load()
    with _lock:
        for p in positions:
            if p["id"] != pos_id:
                continue
            sold = max(1, min(int(contracts_sold), p["contracts"]))
            realized = None
            if exit_value is not None:
                cost_of_sold = p["net_debit"] * 100 * sold
                realized = round(float(exit_value) - cost_of_sold, 2)
                p["realized_pnl"] = round(p.get("realized_pnl", 0.0) + realized, 2)
            p["contracts"] -= sold
            rungs = set(p.get("scaled_out", []))
            rungs.add("t1" if "t1" not in rungs else "t2")
            p["scaled_out"] = sorted(rungs)
            p.setdefault("partial_exits", []).append({
                "contracts": sold,
                "at": datetime.now(timezone.utc).isoformat(),
                "exit_value": exit_value,
                "realized_pnl": realized,
            })
            if p["contracts"] <= 0:
                p["status"] = "closed"
                p["closed_at"] = datetime.now(timezone.utc).isoformat()
        _save(positions)
    return _load()


def update_position(pos_id: str, fields: dict):
    """Correct a position's details to match what actually filled in your broker.

    Needed because the plan is a recommendation, not an execution: if you buy a single
    $85 call when the plan suggested an 80/90 spread, every downstream P&L number and
    exit verdict is computed against a position you don't own. Set `short_strike` to
    null here to convert a mis-recorded spread into the single long option you hold.
    """
    allowed = {"ticker", "kind", "expiry", "long_strike", "short_strike", "net_debit",
               "contracts", "entry_spot", "target", "stop", "strategy", "peak_pnl_pct"}
    positions = _load()
    with _lock:
        for p in positions:
            if p["id"] == pos_id:
                for k, v in fields.items():
                    if k in allowed:
                        p[k] = v
                if "short_strike" in fields and fields["short_strike"] is None:
                    p["strategy"] = f"Long {p['kind'].upper()}"
                p["peak_pnl_pct"] = float(fields.get("peak_pnl_pct", 0.0))
        _save(positions)
    return _load()


def performance_stats() -> dict:
    """Realized track record across closed positions - your actual edge, not backtested."""
    positions = _load()
    closed = [p for p in positions if p.get("status") == "closed"]
    with_pnl = [p for p in closed if p.get("realized_pnl") is not None]
    wins = [p for p in with_pnl if p["realized_pnl"] > 0]
    losses = [p for p in with_pnl if p["realized_pnl"] <= 0]
    total = round(sum(p["realized_pnl"] for p in with_pnl), 2)
    open_ct = len([p for p in positions if p.get("status") == "open"])
    return {
        "closed_total": len(closed),
        "tracked_pnl_count": len(with_pnl),
        "untracked_count": len(closed) - len(with_pnl),
        "open_count": open_ct,
        "realized_pnl": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(with_pnl) * 100) if with_pnl else None,
        "avg_win": round(sum(p["realized_pnl"] for p in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(p["realized_pnl"] for p in losses) / len(losses), 2) if losses else 0,
        "by_strategy": _attribution(with_pnl, "strategies"),
        "by_iv": _attribution(with_pnl, "iv_verdict"),
    }


def _attribution(trades: list[dict], field: str) -> list[dict]:
    """Group realized outcomes by an entry-context field.

    This is the feedback loop: with enough closed trades it answers "does the BB squeeze
    strategy actually make money, or is MACD carrying everything?" and "do the trades I
    entered at rich IV underperform?" - questions no amount of backtesting settles as
    honestly as your own fills.
    """
    buckets: dict[str, list[float]] = {}
    for t in trades:
        ctx = t.get("entry_context") or {}
        val = ctx.get(field)
        keys = val if isinstance(val, list) else ([val] if val else [])
        for k in keys:
            buckets.setdefault(str(k), []).append(t["realized_pnl"])

    out = []
    for k, pnls in buckets.items():
        w = [p for p in pnls if p > 0]
        out.append({
            "key": k,
            "trades": len(pnls),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "win_rate": round(len(w) / len(pnls) * 100),
        })
    out.sort(key=lambda d: -d["total_pnl"])
    return out


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
    """Current per-share value of the position. Live chain mid if available, else BS.

    Handles BOTH structures:
      - single long option: short_k is None -> value is just the long leg
      - vertical spread:    short_k set     -> value is long leg minus short leg
    """
    single = short_k is None
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

        lm = mid(long_k)
        if single:
            if lm is not None:
                return max(0.0, lm)
        else:
            sm = mid(short_k)
            if lm is not None and sm is not None:
                return max(0.0, lm - sm)
    except Exception:
        pass
    # Fallback: Black-Scholes
    iv = _realized_iv(ticker)
    long_val = black_scholes(spot, long_k, dte, iv, kind)
    if single:
        return max(0.0, long_val)
    return max(0.0, long_val - black_scholes(spot, short_k, dte, iv, kind))


def _evaluate_one(p: dict) -> dict:
    spot = get_current_price(p["ticker"]) or p["entry_spot"]
    today = datetime.now(timezone.utc).date()
    exp = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
    dte = (exp - today).days
    opened = datetime.fromisoformat(p["opened_at"]).date()
    days_held = (today - opened).days

    short_k = p.get("short_strike")          # None => single long option
    entry_debit = p["net_debit"]
    if short_k is None:
        # A long option has no capped max - use the engine's price target as the
        # reference "full move" so pct_of_max stays meaningful for the UI.
        tgt_intrinsic = (max(0.0, p["target"] - p["long_strike"]) if p["kind"] == "call"
                         else max(0.0, p["long_strike"] - p["target"]))
        max_profit = max(0.01, tgt_intrinsic - entry_debit)
    else:
        max_profit = max(0.01, abs(short_k - p["long_strike"]) - entry_debit)

    mark = _spread_mark(p["ticker"], p["expiry"], p["long_strike"], short_k,
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

    # Which ladder rungs have already been taken on this position.
    done = set(p.get("scaled_out", []))
    trailing = (peak_pnl_pct >= TRAIL_ARM_PCT
                and pnl_pct <= trail_exit_level(peak_pnl_pct))

    sell_contracts = contracts   # default: exit everything

    if broke_stop or pnl_pct <= -STOP_LOSS_PCT * 100:
        action, urgency = "SELL", "now"
        reason = ("Underlying broke your stop level. " if broke_stop else
                  f"Down {pnl_pct:.0f}% on premium. ") + "Cut it — sell at next market open."
    elif trailing:
        action, urgency = "SELL", "now"
        reason = (f"Peaked at +{peak_pnl_pct:.0f}%, now +{pnl_pct:.0f}% — it's giving the gain "
                  "back. Take what's left at next open rather than round-tripping to zero.")
    elif pnl_pct >= TIER3_PCT:
        action, urgency = "SELL", "now"
        reason = (f"Up {pnl_pct:.0f}% — close the runner. This is already a great trade; "
                  "holding for more is where good options trades turn into round trips.")
    elif contracts == 1 and pnl_pct >= SINGLE_CT_TAKE_PCT and "single" not in done:
        # One contract can't be split, so the ladder collapses to a single decision.
        action, urgency = "SELL", "now"
        reason = (f"Up {pnl_pct:.0f}% on a single contract — you can't scale out of one, "
                  "so take the whole profit now. A 30% option gain in a few days is a good "
                  "trade; don't wait for the double.")
    elif contracts >= 2 and pnl_pct >= TIER2_PCT and "t2" not in done:
        action, urgency = "SCALE", "now"
        sell_contracts = max(1, (contracts + 1) // 2)
        reason = (f"Up {pnl_pct:.0f}% — take tier 2: sell {sell_contracts} of {contracts}. "
                  "Bank the bulk of the move, keep a runner in case it keeps going.")
    elif contracts >= 2 and pnl_pct >= TIER1_PCT and "t1" not in done:
        action, urgency = "SCALE", "now"
        sell_contracts = contracts // 2
        reason = (f"Up {pnl_pct:.0f}% — take tier 1: sell {sell_contracts} of {contracts}. "
                  "This locks in real money and makes the rest of the trade close to free.")
    elif hit_target or pct_of_max >= TAKE_PROFIT_PCT:
        action, urgency = "SELL", "now"
        reason = (f"Price target reached (~{max(0,pct_of_max)*100:.0f}% of the expected move). "
                  "Lock it in at next market open.")
    elif dte <= TIME_STOP_DTE:
        action, urgency = "SELL", "now"
        reason = (f"Only {dte} days to expiry — theta accelerates hard from here. "
                  "Close it at next market open regardless of P&L.")
    elif days_held >= MAX_HOLD_DAYS:
        action, urgency = "SELL", "now"
        reason = (f"Held {days_held} days — past the swing window. "
                  "Take what's there and recycle the capital.")
    elif flipped:
        action, urgency = "SELL", "now"
        reason = "The stock's signal flipped against you — thesis broken. Exit at next open."
    else:
        next_rung = (SINGLE_CT_TAKE_PCT if contracts == 1 else
                     (TIER1_PCT if "t1" not in done else TIER2_PCT))
        if peak_pnl_pct >= TRAIL_ARM_PCT and pnl_pct < peak_pnl_pct:
            reason = (f"Hold, but watch it — peaked at +{peak_pnl_pct:.0f}%, now +{pnl_pct:.0f}%. "
                      f"Flags SELL if it slips to +{trail_exit_level(peak_pnl_pct):.0f}%.")
        elif pnl_pct > 0:
            reason = (f"Hold — up {pnl_pct:.0f}%, {days_held}d held, {dte}d to expiry. "
                      f"Next action at +{next_rung:.0f}%.")
        else:
            reason = (f"Hold. {days_held}d held, {dte}d to expiry, down {abs(pnl_pct):.0f}% — "
                      f"still inside the {STOP_LOSS_PCT*100:.0f}% stop. Let it work.")

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
        "sell_contracts": sell_contracts if action in ("SELL", "SCALE") else 0,
        "structure": "single" if short_k is None else "spread",
        "scaled_out": sorted(done),
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
