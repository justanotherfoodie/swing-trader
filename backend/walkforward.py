"""
Walk-forward backtest: the test that can kill this strategy.

The single-window backtester answers "what happened that week". This answers the only
question that matters before risking money: DOES THIS HAVE AN EDGE, once you account for
what trading actually costs?

How it works
------------
Roll an entry date forward through years of history. At each date, score every ticker
using ONLY bars up to that day (no look-ahead), take the best signals, open positions,
then replay the real profit ladder against the actual price path that followed. Repeat a
few hundred times and look at the distribution rather than one lucky week.

What makes it honest
--------------------
  * No look-ahead. Indicators are precomputed once over full history, but each entry date
    only ever reads rows at or before itself, and exits only read rows after.
  * Costs are real. Commission per contract each way, plus slippage modelled as a share
    of the bid/ask spread you cross. On a small account these are not a rounding error -
    they are frequently the whole edge.
  * Expectancy, not win rate. A 35% win rate with 3:1 winners is a good business. A 60%
    win rate with 2:1 losers is a slow bleed. Only expectancy distinguishes them.
  * Results split by market regime, so "it works" can be qualified with "...when".

Option pricing uses Black-Scholes with each stock's trailing realized volatility, held
constant across the hold. Free data has no historical option chains, so this is the
available approximation. It captures directional P&L and time decay faithfully; it does
NOT capture IV expansion or crush, which is stated plainly rather than papered over.

Usage:
  python walkforward.py                          # ~2y, 5-day holds, single long options
  python walkforward.py --years 3 --hold-days 10
  python walkforward.py --structure spread
  python walkforward.py --universe 300 --step 3  # more tickers, denser entry dates
"""

import argparse
import concurrent.futures
import math
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

from data.fetcher import get_ohlcv
from data.universe import get_universe
from signals.indicators import add_all_indicators
from signals.scorer import score_ticker
from signals.options import (black_scholes, value_vertical, round_to_strike,
                             strike_increment)
from portfolio import (TIER1_PCT, TIER2_PCT, TIER3_PCT, SINGLE_CT_TAKE_PCT,
                       TRAIL_ARM_PCT, trail_exit_level, STOP_LOSS_PCT)

ENTRY_DTE = 35

# ---- Real-world trading costs -------------------------------------------------
# Retail options commission is typically ~$0.65/contract each way. Slippage is the
# bigger cost: you rarely fill at the mid, so we assume you give up a share of the
# half-spread entering AND exiting. These two numbers decide whether a thin edge
# survives contact with reality.
COMMISSION_PER_CONTRACT = 0.65
SLIPPAGE_FRAC_OF_PRICE = 0.02   # ~2% of premium each way; conservative for liquid strikes


def realized_vol(closes: pd.Series, window: int = 20) -> float:
    r = np.log(closes / closes.shift(1)).dropna().tail(window)
    if len(r) < 5:
        return 0.30
    return max(0.10, min(2.0, float(r.std() * math.sqrt(252))))


def _price(spot, long_k, short_k, dte, iv, kind):
    if short_k is None:
        return black_scholes(spot, long_k, dte, iv, kind)
    return value_vertical(spot, long_k, short_k, dte, iv, kind)


def _strikes(signal, entry, target, structure):
    kind = "call" if signal == "BUY" else "put"
    if structure == "single":
        return round_to_strike(entry * (0.97 if kind == "call" else 1.03)), None, kind
    inc = strike_increment(entry)
    long_k = round_to_strike(entry)
    short_k = round_to_strike(target)
    if kind == "call" and short_k <= long_k:
        short_k = long_k + inc
    if kind == "put" and short_k >= long_k:
        short_k = long_k - inc
    return long_k, short_k, kind


def simulate(df, entry_date, long_k, short_k, kind, iv, entry_debit,
             contracts, hold_days):
    """Replay the live exit ladder over the real price path. Returns net P&L after costs."""
    future = df[df.index > entry_date].head(hold_days)
    if future.empty or entry_debit <= 0:
        return None

    remaining = contracts
    gross = 0.0
    peak = 0.0
    rungs: set[str] = set()
    exit_reason = "TIME"
    days = 0

    for _, bar in future.iterrows():
        days += 1
        spot = float(bar["close"])
        val = _price(spot, long_k, short_k, max(0, ENTRY_DTE - days), iv, kind)
        pnl_pct = (val - entry_debit) / entry_debit * 100
        peak = max(peak, pnl_pct)

        def bank(n, why):
            nonlocal remaining, gross, exit_reason
            n = min(n, remaining)
            if n <= 0:
                return
            gross += (val - entry_debit) * 100 * n
            remaining -= n
            exit_reason = why

        if pnl_pct <= -STOP_LOSS_PCT * 100:
            bank(remaining, "STOP")
        elif peak >= TRAIL_ARM_PCT and pnl_pct <= trail_exit_level(peak):
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

    if remaining > 0:
        spot = float(future["close"].iloc[-1])
        val = _price(spot, long_k, short_k, max(0, ENTRY_DTE - days), iv, kind)
        gross += (val - entry_debit) * 100 * remaining

    # Costs: commission both ways on every contract, plus slippage on entry and exit.
    legs = 1 if short_k is None else 2
    commission = COMMISSION_PER_CONTRACT * contracts * legs * 2
    slippage = entry_debit * 100 * contracts * SLIPPAGE_FRAC_OF_PRICE * 2
    net = gross - commission - slippage

    return {
        "gross": round(gross, 2), "net": round(net, 2),
        "costs": round(commission + slippage, 2),
        "peak_pct": round(peak, 1), "exit": exit_reason, "days": days,
        "cost_basis": round(entry_debit * 100 * contracts, 2),
    }


def regime_at(spy: pd.DataFrame, date) -> str:
    """Classify the market on a past date - no look-ahead, only bars up to `date`."""
    h = spy[spy.index <= date]
    if len(h) < 210:
        return "unknown"
    c = h["close"]
    px = float(c.iloc[-1])
    e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    e200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1])
    # Path efficiency over the last month: net travel vs total travel.
    w = c.tail(21)
    path = float(w.diff().abs().sum())
    eff = float(abs(w.iloc[-1] - w.iloc[0]) / path) if path > 0 else 0.0
    if eff < 0.25:
        return "chop"
    if px > e50 > e200:
        return "bull_trend"
    if px < e50 < e200:
        return "bear_trend"
    return "mixed"


def run(years=2, hold_days=5, budget=600.0, universe_size=200, step=5,
        structure="single", picks=2, verbose=True):
    print(f"\n{'='*74}")
    print(f"  WALK-FORWARD BACKTEST - {years}y, {hold_days}-day holds, "
          f"{structure} options, ${budget:,.0f}/window")
    print(f"  Costs: ${COMMISSION_PER_CONTRACT}/contract/side + "
          f"{SLIPPAGE_FRAC_OF_PRICE*100:.0f}% slippage each way")
    print(f"{'='*74}")

    period = f"{max(2, years + 1)}y"
    tickers = get_universe()[:universe_size]

    def load(tk):
        df = get_ohlcv(tk, period=period)
        if df is None or df.empty or len(df) < 260:
            return tk, None
        try:
            return tk, add_all_indicators(df)   # computed ONCE per ticker
        except Exception:
            return tk, None

    data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for tk, df in ex.map(load, tickers):
            if df is not None:
                data[tk] = df
    if not data:
        print("No data.")
        return

    spy = get_ohlcv("SPY", period=period)
    calendar = sorted(set().union(*[set(df.index) for df in list(data.values())[:40]]))
    # Leave room at the start for indicator warm-up and at the end for the hold.
    usable = calendar[220:-(hold_days + 1)]
    entry_dates = usable[::step]
    print(f"  {len(data)} tickers | {len(entry_dates)} entry dates "
          f"({entry_dates[0].date()} -> {entry_dates[-1].date()})\n")

    trades: list[dict] = []
    for i, d in enumerate(entry_dates):
        reg = regime_at(spy, d) if spy is not None and not spy.empty else "unknown"
        cands = []
        for tk, df in data.items():
            hist = df[df.index <= d]
            if len(hist) < 210 or d not in df.index:
                continue
            sig = score_ticker(tk, hist, 0.0, indicators_ready=True)
            if sig is None or sig.signal == "WATCH":
                continue
            cands.append((tk, sig, hist))

        if not cands:
            continue
        qrank = {"high": 0, "medium": 1, "low": 2}
        cands.sort(key=lambda c: (qrank.get(c[1].quality, 3), -c[1].confidence))

        # Take the best few, one per ticker, split across both directions.
        chosen, seen_dir = [], defaultdict(int)
        for tk, sig, hist in cands:
            if seen_dir[sig.signal] >= picks:
                continue
            chosen.append((tk, sig, hist))
            seen_dir[sig.signal] += 1
            if len(chosen) >= picks * 2:
                break

        per_trade = budget / max(1, len(chosen))
        for tk, sig, hist in chosen:
            iv = realized_vol(hist["close"])
            spot = float(hist["close"].iloc[-1])
            long_k, short_k, kind = _strikes(sig.signal, spot, sig.take_profit_1, structure)
            debit = _price(spot, long_k, short_k, ENTRY_DTE, iv, kind)
            if debit <= 0.05:
                continue
            n = int(per_trade / (debit * 100))
            if n < 1:
                continue
            res = simulate(data[tk], d, long_k, short_k, kind, iv, debit, n, hold_days)
            if res is None:
                continue
            trades.append({
                "date": d, "ticker": tk, "signal": sig.signal, "kind": kind,
                "regime": reg, "quality": sig.quality, "confidence": sig.confidence,
                "strategies": [r.name for r in sig.strategy_results if r.score],
                **res,
            })

        if verbose and (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(entry_dates)} windows, {len(trades)} trades so far")

    if not trades:
        print("No trades generated.")
        return
    _report(trades, budget, structure)
    return trades


def _stats(pnls: list[float], bases: list[float]) -> dict:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n if n else 0
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0   # negative
    # Expectancy: average dollars per trade. THE number that decides if this is a business.
    expectancy = win_rate * avg_w + (1 - win_rate) * avg_l
    total = sum(pnls)
    invested = sum(bases)
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    return {
        "n": n, "total": total, "win_rate": win_rate * 100,
        "avg_win": avg_w, "avg_loss": avg_l, "expectancy": expectancy,
        "return_on_capital": (total / invested * 100) if invested else 0,
        "profit_factor": (gross_w / gross_l) if gross_l else float("inf"),
    }


def _report(trades: list[dict], budget: float, structure: str):
    pnls = [t["net"] for t in trades]
    bases = [t["cost_basis"] for t in trades]
    gross_pnls = [t["gross"] for t in trades]
    s = _stats(pnls, bases)
    sg = _stats(gross_pnls, bases)
    costs = sum(t["costs"] for t in trades)

    print(f"\n{'='*74}")
    print("  OVERALL")
    print(f"{'='*74}")
    print(f"  Trades:            {s['n']}")
    print(f"  Win rate:          {s['win_rate']:.1f}%")
    print(f"  Average win:       ${s['avg_win']:+,.2f}")
    print(f"  Average loss:      ${s['avg_loss']:+,.2f}")
    print(f"  EXPECTANCY/trade:  ${s['expectancy']:+,.2f}   <-- the number that matters")
    print(f"  Profit factor:     {s['profit_factor']:.2f}   (>1.0 = profitable)")
    print(f"  Return on capital: {s['return_on_capital']:+.1f}%")
    print(f"  Net P&L:           ${s['total']:+,.2f}")
    print()
    print(f"  Before costs:      ${sg['total']:+,.2f}  (expectancy ${sg['expectancy']:+,.2f})")
    print(f"  Total costs:       ${costs:,.2f}")
    print(f"  --> costs turned {'a winner into a loser' if sg['total'] > 0 >= s['total'] else 'the result ' + ('worse' if costs else 'unchanged')}")

    # Equity curve / drawdown, in trade order.
    eq, peak, maxdd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x["date"]):
        eq += t["net"]
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
    print(f"  Max drawdown:      ${maxdd:,.2f}")

    def bucket(field, title, keyfn=None):
        print(f"\n{'-'*74}")
        print(f"  BY {title}")
        print(f"{'-'*74}")
        groups: dict = defaultdict(lambda: ([], []))
        for t in trades:
            keys = keyfn(t) if keyfn else [t[field]]
            for k in keys:
                groups[k][0].append(t["net"])
                groups[k][1].append(t["cost_basis"])
        rows = [(k, _stats(p, b)) for k, (p, b) in groups.items()]
        rows.sort(key=lambda r: -r[1]["expectancy"])
        print(f"  {'':<22} {'trades':>7} {'win%':>7} {'expectancy':>12} {'total':>12}")
        for k, st in rows:
            print(f"  {str(k):<22} {st['n']:>7} {st['win_rate']:>6.1f}% "
                  f"{st['expectancy']:>+11.2f} {st['total']:>+11.2f}")

    bucket("regime", "MARKET REGIME")
    bucket("quality", "SIGNAL QUALITY")
    bucket("kind", "DIRECTION")
    bucket(None, "STRATEGY (a trade counts once per strategy that fired)",
           keyfn=lambda t: t["strategies"] or ["none"])
    bucket("exit", "EXIT REASON")

    print(f"\n{'='*74}")
    verdict = ("EDGE PRESENT after costs." if s["expectancy"] > 0 else
               "NO EDGE after costs - this strategy loses money as configured.")
    print(f"  VERDICT: {verdict}")
    print(f"{'='*74}")
    print("  Caveats: Black-Scholes pricing with realized-vol as IV proxy, held constant.")
    print("  Does not model IV expansion/crush or assignment. Past results do not")
    print("  guarantee anything about the future. Not financial advice.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Walk-forward backtest with real costs.")
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--budget", type=float, default=600.0)
    ap.add_argument("--universe", type=int, default=200)
    ap.add_argument("--step", type=int, default=5, help="trading days between entry dates")
    ap.add_argument("--structure", choices=["single", "spread"], default="single")
    ap.add_argument("--picks", type=int, default=2, help="positions per direction per window")
    a = ap.parse_args()
    run(years=a.years, hold_days=a.hold_days, budget=a.budget,
        universe_size=a.universe, step=a.step, structure=a.structure, picks=a.picks)
