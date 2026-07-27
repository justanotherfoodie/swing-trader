"""
Options strategy backtester.

Goal: with a fixed budget (default $600), simulate trading the engine's option spreads
over a short hold (default 5 trading days ~ a week, e.g. June 23 -> June 30) and report
the hypothetical profit using REAL historical stock prices.

How it stays honest:
  - Signals are generated AS OF the entry date using only data up to that day (the price
    series is sliced, so there is no look-ahead / future peeking).
  - Spreads are priced with Black-Scholes at BOTH entry and exit using each stock's own
    trailing realized volatility. P&L is therefore driven by the stock's ACTUAL price move
    over the window (plus time decay), which is exactly what we want to test.
  - Assumptions disclosed: implied vol is approximated by realized vol and held constant
    across the hold; commissions/slippage ignored; European-style exercise. Real fills
    will differ, but the directional result is representative.

yfinance does not provide historical option chains, so modelling the spread is the only
way to backtest options on free data. The model is self-consistent (same IV at entry/exit),
so it isolates the effect of the move the engine was trying to capture.

Usage:
  python backtester.py                      # last completed 5-day window, $600
  python backtester.py --budget 1000 --hold-days 5
  python backtester.py --as-of 2026-06-13   # explicit entry date
  python backtester.py --max-tickers 400    # faster (scan fewer names)
"""

import argparse
import concurrent.futures
import math
import numpy as np
import pandas as pd

from data.fetcher import get_ohlcv
from data.universe import get_universe
from signals.scorer import score_ticker
from signals.options import (value_vertical, round_to_strike, strike_increment,
                             black_scholes)

ENTRY_DTE = 35  # assume a ~35-day option is bought at entry


def realized_iv(closes: pd.Series, window: int = 20) -> float:
    """Annualized realized volatility from trailing daily log returns (IV proxy)."""
    rets = np.log(closes / closes.shift(1)).dropna().tail(window)
    if len(rets) < 5:
        return 0.30
    iv = float(rets.std() * math.sqrt(252))
    return max(0.10, min(2.0, iv))  # clamp to sane bounds


def build_spread_strikes(signal: str, entry: float, target: float, structure: str = "spread"):
    """Pick listed-style strikes. `structure="single"` returns short_k=None (long option)."""
    inc = strike_increment(entry)
    kind = "call" if signal == "BUY" else "put"
    if structure == "single":
        # Mirror the live engine: a slightly ITM strike (~0.6 delta) rather than ATM.
        long_k = round_to_strike(entry * (0.97 if kind == "call" else 1.03))
        return long_k, None, kind
    long_k = round_to_strike(entry)
    if signal == "BUY":  # bull call: short above long, near target
        short_k = round_to_strike(target)
        if short_k <= long_k:
            short_k = long_k + inc
        return long_k, short_k, "call"
    else:                # bear put: short below long, near target
        short_k = round_to_strike(target)
        if short_k >= long_k:
            short_k = long_k - inc
        return long_k, short_k, "put"


def simulate_with_ladder(df, entry_date, exit_date, long_k, short_k, kind, iv,
                         entry_debit, entry_dte, contracts):
    """Walk the position forward day by day, applying the live profit ladder.

    The old backtest just held to a fixed exit date, which measures the signal but not
    the strategy: the real system scales out at +25%/+50%, trails a peak, and cuts at
    -40%. Replaying those rules against the actual price path is the only way to know
    whether the exit logic helps or hurts.
    """
    from portfolio import (TIER1_PCT, TIER2_PCT, TIER3_PCT, SINGLE_CT_TAKE_PCT,
                           TRAIL_ARM_PCT, trail_exit_level, STOP_LOSS_PCT)

    path = df[(df.index > entry_date) & (df.index <= exit_date)]
    remaining = contracts
    realized = 0.0
    peak_pct = 0.0
    rungs: set[str] = set()
    events: list[str] = []
    days = 0

    for ts, bar in path.iterrows():
        days += 1
        spot = float(bar["close"])
        dte = max(0, entry_dte - days)
        val = value_vertical(spot, long_k, short_k, dte, iv, kind) if short_k is not None \
            else black_scholes(spot, long_k, dte, iv, kind)
        pnl_pct = (val - entry_debit) / entry_debit * 100 if entry_debit else 0
        peak_pct = max(peak_pct, pnl_pct)

        def bank(n, why):
            nonlocal remaining, realized
            n = min(n, remaining)
            if n <= 0:
                return
            realized += (val - entry_debit) * 100 * n
            remaining -= n
            events.append(f"d{days} {why} {n}ct @ {pnl_pct:+.0f}%")

        if pnl_pct <= -STOP_LOSS_PCT * 100:
            bank(remaining, "STOP")
        elif peak_pct >= TRAIL_ARM_PCT and pnl_pct <= trail_exit_level(peak_pct):
            bank(remaining, "TRAIL")
        elif pnl_pct >= TIER3_PCT:
            bank(remaining, "TIER3")
        elif contracts == 1 and pnl_pct >= SINGLE_CT_TAKE_PCT:
            bank(remaining, "TAKE")
        elif contracts >= 2 and pnl_pct >= TIER2_PCT and "t2" not in rungs:
            rungs.add("t2"); bank(max(1, remaining // 2), "TIER2")
        elif contracts >= 2 and pnl_pct >= TIER1_PCT and "t1" not in rungs:
            rungs.add("t1"); bank(contracts // 2, "TIER1")

        if remaining <= 0:
            break

    # Anything still open is marked out at the final bar.
    if remaining > 0 and len(path):
        spot = float(path["close"].iloc[-1])
        dte = max(0, entry_dte - days)
        val = value_vertical(spot, long_k, short_k, dte, iv, kind) if short_k is not None \
            else black_scholes(spot, long_k, dte, iv, kind)
        realized += (val - entry_debit) * 100 * remaining
        events.append(f"d{days} EXPIRE-EXIT {remaining}ct @ "
                      f"{(val-entry_debit)/entry_debit*100 if entry_debit else 0:+.0f}%")
    return round(realized, 2), events, round(peak_pct, 1)


class Position:
    def __init__(self, ticker, signal, kind, long_k, short_k, entry_debit,
                 contracts, entry_spot, target):
        self.ticker = ticker
        self.signal = signal
        self.kind = kind
        self.long_k = long_k
        self.short_k = short_k
        self.entry_debit = entry_debit          # per share
        self.contracts = contracts
        self.entry_spot = entry_spot
        self.target = target
        self.exit_value = None                  # per share
        self.exit_spot = None
        self.pnl = None

    @property
    def cost(self):
        return round(self.entry_debit * 100 * self.contracts, 2)


def run_backtest(budget=600.0, hold_days=5, as_of=None, max_tickers=900,
                 picks_per_side=1, structure="single"):
    print(f"\n{'='*64}")
    print(f"  OPTIONS STRATEGY BACKTEST  —  budget ${budget:,.0f}")
    print(f"{'='*64}")

    tickers = get_universe()[:max_tickers]

    # Fetch ~1y so we have enough history before the entry date and the exit bar after.
    def fetch(tk):
        df = get_ohlcv(tk, period="1y")
        return tk, df

    data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for tk, df in ex.map(fetch, tickers):
            if df is not None and not df.empty and len(df) > 60:
                data[tk] = df

    if not data:
        print("No price data available.")
        return

    # Determine entry/exit dates from the common latest available date.
    latest = max(df.index[-1] for df in data.values())
    # Use a reference ticker's calendar (SPY-like): just use any df's index union.
    cal = sorted(set().union(*[set(df.index) for df in list(data.values())[:50]]))
    cal = [d for d in cal if d <= latest]

    if as_of:
        entry_date = pd.Timestamp(as_of)
        # exit = hold_days trading days later
        future = [d for d in cal if d > entry_date]
        if len(future) < hold_days:
            print(f"Not enough data after {as_of} for a {hold_days}-day hold.")
            return
        exit_date = future[hold_days - 1]
    else:
        if len(cal) < hold_days + 1:
            print("Not enough trading days available.")
            return
        exit_date = cal[-1]
        entry_date = cal[-1 - hold_days]

    print(f"  Entry: {entry_date.date()}   Exit: {exit_date.date()}   "
          f"({hold_days} trading days)")
    print(f"  Pricing: Black-Scholes, IV = trailing realized vol (held constant)")
    print(f"  Scanned {len(data)} stocks for signals as of entry date\n")

    # Generate signals AS OF entry date (no look-ahead).
    buys, sells = [], []
    for tk, df in data.items():
        hist = df[df.index <= entry_date]
        if len(hist) < 60 or exit_date not in df.index or entry_date not in df.index:
            continue
        sig = score_ticker(tk, hist.copy(), 0.0)
        if sig is None or sig.signal == "WATCH":
            continue
        entry_spot = float(df.loc[entry_date, "close"])
        exit_spot = float(df.loc[exit_date, "close"])
        iv = realized_iv(hist["close"])
        rec = (sig, entry_spot, exit_spot, iv)
        (buys if sig.signal == "BUY" else sells).append(rec)

    # Rank by quality then confidence.
    qrank = {"high": 0, "medium": 1, "low": 2}
    buys.sort(key=lambda r: (qrank[r[0].quality], -r[0].confidence))
    sells.sort(key=lambda r: (qrank[r[0].quality], -r[0].confidence))

    chosen = buys[:picks_per_side] + sells[:picks_per_side]
    if not chosen:
        print("No actionable BUY/SELL signals on the entry date.")
        return

    # Build positions and allocate budget by confidence weight.
    drafts = []
    for sig, entry_spot, exit_spot, iv in chosen:
        long_k, short_k, kind = build_spread_strikes(sig.signal, entry_spot,
                                                     sig.take_profit_1, structure)
        entry_debit = (value_vertical(entry_spot, long_k, short_k, ENTRY_DTE, iv, kind)
                       if short_k is not None
                       else black_scholes(entry_spot, long_k, ENTRY_DTE, iv, kind))
        if entry_debit <= 0.01:
            continue
        drafts.append((sig, entry_spot, exit_spot, iv, long_k, short_k, kind, entry_debit))

    if not drafts:
        print("No tradeable positions could be constructed.")
        return

    # Rank drafts (best first) and build Position objects with 0 contracts.
    drafts.sort(key=lambda d: (qrank[d[0].quality], -d[0].confidence))
    exit_dte = ENTRY_DTE - hold_days
    positions = []
    for sig, entry_spot, exit_spot, iv, long_k, short_k, kind, entry_debit in drafts:
        pos = Position(sig.ticker, sig.signal, kind, long_k, short_k,
                       round(entry_debit, 2), 0, entry_spot, sig.take_profit_1)
        pos.exit_spot = exit_spot
        pos.exit_value = round(
            value_vertical(exit_spot, long_k, short_k, exit_dte, iv, kind)
            if short_k is not None
            else black_scholes(exit_spot, long_k, exit_dte, iv, kind), 2)
        pos.iv = iv
        pos.df = data[sig.ticker]
        positions.append(pos)

    # Balanced allocation: seed 1 contract of each pick (best first) to keep the
    # call/put hedge intact, THEN round-robin add contracts so no single cheap
    # position swallows the whole budget.
    cash_used = 0.0
    for pos in positions:
        pc = pos.entry_debit * 100
        if cash_used + pc <= budget:
            pos.contracts = 1
            cash_used += pc
    improved = True
    while improved:
        improved = False
        for pos in positions:
            if pos.contracts < 1:
                continue
            pc = pos.entry_debit * 100
            if cash_used + pc <= budget:
                pos.contracts += 1
                cash_used += pc
                improved = True

    positions = [p for p in positions if p.contracts > 0]
    if not positions:
        print(f"Budget ${budget:.0f} too small for any position at current prices.")
        return

    # P&L two ways: buy-and-hold to the exit date vs the live profit ladder replayed
    # over the same price path. The difference IS the value of the exit logic.
    for p in positions:
        p.hold_pnl = round((p.exit_value - p.entry_debit) * 100 * p.contracts, 2)
        p.pnl, p.events, p.peak = simulate_with_ladder(
            p.df, entry_date, exit_date, p.long_k, p.short_k, p.kind, p.iv,
            p.entry_debit, ENTRY_DTE, p.contracts)

    _print_report(positions, budget, cash_used, entry_date, exit_date, structure)
    return positions


def _print_report(positions, budget, cash_used, entry_date, exit_date, structure="spread"):
    calls = [p for p in positions if p.kind == "call"]
    puts = [p for p in positions if p.kind == "put"]

    def line(p):
        move = (p.exit_spot - p.entry_spot) / p.entry_spot * 100
        strikes = f"{p.long_k:g}" if p.short_k is None else f"{p.long_k:g}/{p.short_k:g}"
        return (f"  {p.ticker:6} {p.kind.upper()} {strikes:12} x{p.contracts:>2}ct  "
                f"stock {move:+5.1f}%  peak {p.peak:+5.0f}%  "
                f"ladder {'+' if p.pnl >= 0 else ''}{p.pnl:>7,.0f}  "
                f"(hold {'+' if p.hold_pnl >= 0 else ''}{p.hold_pnl:,.0f})\n"
                f"         {' | '.join(p.events) if p.events else 'no exits triggered'}")

    label = "LONG CALLS" if structure == "single" else "CALL SPREADS"
    plabel = "LONG PUTS" if structure == "single" else "PUT SPREADS"
    print(f"{label} (bullish):")
    print("\n".join(line(p) for p in calls) if calls else "  (none)")
    print(f"\n{plabel} (bearish):")
    print("\n".join(line(p) for p in puts) if puts else "  (none)")

    total_call_ct = sum(p.contracts for p in calls)
    total_put_ct = sum(p.contracts for p in puts)
    total_pnl = sum(p.pnl for p in positions)
    hold_pnl = sum(p.hold_pnl for p in positions)
    wins = sum(1 for p in positions if p.pnl > 0)

    print(f"\n{'-'*72}")
    print(f"  Contracts:  {total_call_ct} call  +  {total_put_ct} put")
    print(f"  Capital deployed: ${cash_used:,.0f} / ${budget:,.0f} "
          f"(cash left ${budget-cash_used:,.0f})")
    ret = (total_pnl / cash_used * 100) if cash_used else 0
    hret = (hold_pnl / cash_used * 100) if cash_used else 0
    print(f"  WITH PROFIT LADDER: {'+' if total_pnl>=0 else ''}${total_pnl:,.2f}  "
          f"({'+' if ret>=0 else ''}{ret:.1f}%)")
    print(f"  Buy & hold to exit: {'+' if hold_pnl>=0 else ''}${hold_pnl:,.2f}  "
          f"({'+' if hret>=0 else ''}{hret:.1f}%)")
    edge = total_pnl - hold_pnl
    print(f"  Ladder edge:        {'+' if edge>=0 else ''}${edge:,.2f}  "
          f"<- what the exit rules added/cost")
    print(f"  Win rate: {wins}/{len(positions)}")
    print(f"{'-'*72}")
    print("  NOTE: Hypothetical. Black-Scholes with realized-vol IV held constant,")
    print("  no commissions/slippage. Real option fills will differ. Not advice.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backtest the options strategy over a short hold.")
    ap.add_argument("--budget", type=float, default=600.0)
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--as-of", type=str, default=None, help="Entry date YYYY-MM-DD")
    ap.add_argument("--max-tickers", type=int, default=900)
    ap.add_argument("--picks-per-side", type=int, default=2,
                    help="How many bullish and bearish positions to take")
    ap.add_argument("--structure", choices=["single", "spread"], default="single",
                    help="single = long calls/puts (what the live app trades); spread = verticals")
    args = ap.parse_args()
    run_backtest(budget=args.budget, hold_days=args.hold_days, as_of=args.as_of,
                 max_tickers=args.max_tickers, picks_per_side=args.picks_per_side,
                 structure=args.structure)
