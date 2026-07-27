"""
Options strategy builder - turns a directional stock signal into a defined-risk
options play using REAL option chain data from yfinance.

For a swing trade (5-10 day hold) the right structure is a VERTICAL DEBIT SPREAD:
  BUY  signal -> Bull Call Spread (buy call near entry, sell call near target)
  SELL signal -> Bear Put Spread  (buy put near entry,  sell put  near target)

Why a spread instead of a naked call/put:
  - Defined, capped risk (you can only lose the net premium paid)
  - The short leg (sold at our profit target) pays for part of the long leg, so the
    position is cheaper and time decay hurts far less than a lone long option.
  - Max gain is capped at our take-profit target - which is exactly where the stock
    engine already told us to exit. The structure and the thesis line up.

Also builds a PROTECTIVE PUT (insurance) for traders who buy the actual shares.

All pricing is conservative: you PAY the ask and RECEIVE the bid (realistic fills).
If a chain is missing or too illiquid (common for small mid-caps), we return None
and the UI simply shows "no liquid options" rather than a fantasy quote.
"""

from dataclasses import dataclass
from datetime import datetime
import math
import yfinance as yf


@dataclass
class OptionLeg:
    action: str        # "BUY" | "SELL"
    type: str          # "CALL" | "PUT"
    strike: float
    price: float       # per-share premium used in the calc


@dataclass
class OptionsPlay:
    strategy: str            # e.g. "Bull Call Spread"
    expiry: str
    dte: int
    legs: list[OptionLeg]
    net_debit: float         # per share (x100 = per contract $)
    max_profit: float        # per share
    max_loss: float          # per share (= net_debit for a debit spread)
    breakeven: float
    risk_reward: float
    prob_profit: int         # 0-100 estimate
    contracts: int           # for the account risk budget
    cost: float              # contracts * net_debit * 100
    capital_vs_stock: str    # leverage note vs buying shares
    note: str
    quoted_at: str = ""       # ISO timestamp this quote was fetched
    max_spread_pct: float = 0.0   # widest leg bid/ask spread as % of mid (liquidity gauge)
    wide_market: bool = False     # True if a leg's spread is wide enough that fills will slip


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes(spot: float, strike: float, dte: int, iv: float,
                  kind: str = "call", r: float = 0.0) -> float:
    """Black-Scholes price of a European option. Used by the backtester to value
    spreads at entry and exit when no historical option chain is available."""
    if dte <= 0:  # at/after expiry -> intrinsic value
        return max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    if iv <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    T = dte / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if kind == "call":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * T) * _norm_cdf(d2)
    return strike * math.exp(-r * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def value_vertical(spot: float, long_k: float, short_k: float, dte: int, iv: float,
                   kind: str = "call") -> float:
    """Net value (per share) of a vertical debit spread = long leg - short leg."""
    return black_scholes(spot, long_k, dte, iv, kind) - black_scholes(spot, short_k, dte, iv, kind)


def strike_increment(price: float) -> float:
    """Typical listed strike spacing for a given share price."""
    if price < 25:
        return 1.0
    if price < 100:
        return 2.5
    if price < 250:
        return 5.0
    return 10.0


def round_to_strike(price: float) -> float:
    inc = strike_increment(price)
    return round(price / inc) * inc


def _prob_above(spot: float, strike: float, iv: float, dte: int) -> float:
    """Lognormal probability spot finishes ABOVE strike at expiry (r=0)."""
    if spot <= 0 or strike <= 0 or iv <= 0 or dte <= 0:
        return 0.0
    T = dte / 365.0
    d2 = (math.log(spot / strike) - 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return _norm_cdf(d2)


def _pick_expiry(exps: tuple, min_dte: int = 25, max_dte: int = 50):
    today = datetime.now().date()
    best = None
    for e in exps:
        d = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if min_dte <= d <= max_dte:
            return e, d
        if best is None and d >= min_dte:
            best = (e, d)
    if best:
        return best
    if exps:
        e = exps[min(4, len(exps) - 1)]
        return e, (datetime.strptime(e, "%Y-%m-%d").date() - today).days
    return None, 0


def _leg_price(row, side: str) -> float:
    """Conservative fill: pay ask to buy, receive bid to sell; fall back to last/mid."""
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    last = float(row.get("lastPrice") or 0)
    if side == "buy":
        if ask > 0:
            return ask
        if last > 0:
            return last
        return bid * 1.05 if bid > 0 else 0.0
    else:  # sell
        if bid > 0:
            return bid
        if last > 0:
            return last
        return ask * 0.95 if ask > 0 else 0.0


WIDE_MARKET_THRESHOLD = 0.20  # a leg's (ask-bid)/mid beyond this means real slippage vs our quote


def _leg_spread_pct(row) -> float:
    """(ask-bid)/mid for one leg. Large values mean the quote won't hold at fill time -
    this is the actual cause of 'the app's price wasn't accurate': thin option chains
    have wide bid/ask, so the theoretical mid/ask price and your real fill diverge."""
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    if bid <= 0 or ask <= 0:
        return 1.0  # no two-sided market at all - treat as maximally unreliable
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else 1.0


def build_options_play(ticker: str, signal: str, entry: float, target: float,
                       stop: float, risk_budget: float = 200.0) -> OptionsPlay | None:
    if signal not in ("BUY", "SELL"):
        return None
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None
        expiry, dte = _pick_expiry(exps)
        if not expiry:
            return None
        chain = t.option_chain(expiry)
    except Exception:
        return None

    try:
        if signal == "BUY":
            return _bull_call_spread(chain.calls, entry, target, dte, expiry, entry, risk_budget)
        else:
            return _bear_put_spread(chain.puts, entry, target, dte, expiry, entry, risk_budget)
    except Exception:
        return None


def _nearest(df, strike_target):
    idx = (df["strike"] - strike_target).abs().idxmin()
    return df.loc[idx]


def build_long_option(ticker: str, signal: str, entry: float, target: float,
                      stop: float, risk_budget: float = 200.0,
                      moneyness: float = 1.0) -> "OptionsPlay | None":
    """Single-leg long CALL (bullish) or PUT (bearish) - one contract, one click.

    Why offer this alongside spreads: a spread caps your upside at the short strike and
    requires a multi-leg order most retail apps bury behind an extra UI. A single long
    option is two taps, and when the underlying actually runs it pays several times what
    the capped spread would. The tradeoff is real and must be respected: you can lose
    100% of the premium, and theta bleeds every day you're wrong. That is precisely why
    the exit ladder in portfolio.py scales out early on these rather than holding for a
    home run.

    `moneyness` picks the strike relative to spot: 1.0 = at-the-money, 1.03 = ~3% OTM
    (cheaper, more leverage, lower probability).
    """
    if signal not in ("BUY", "SELL"):
        return None
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None
        expiry, dte = _pick_expiry(exps)
        if not expiry:
            return None
        chain = t.option_chain(expiry)
    except Exception:
        return None

    try:
        is_call = signal == "BUY"
        df = (chain.calls if is_call else chain.puts).sort_values("strike").reset_index(drop=True)
        if df.empty:
            return None

        want = entry * moneyness if is_call else entry * (2 - moneyness)
        row = _nearest(df, want)
        strike = float(row["strike"])
        px = _leg_price(row, "buy")
        if px <= 0:
            return None

        iv = float(row.get("impliedVolatility") or 0)
        breakeven = round(strike + px, 2) if is_call else round(strike - px, 2)
        pop_raw = _prob_above(entry, breakeven, iv, dte)
        pop = int(round((pop_raw if is_call else 1 - pop_raw) * 100))

        # Value if the underlying reaches the engine's price target by expiry (intrinsic
        # floor - real value would be higher with time premium left, so this is conservative).
        intrinsic_at_target = max(0.0, (target - strike) if is_call else (strike - target))
        upside_pct = round((intrinsic_at_target - px) / px * 100) if px > 0 else 0

        contracts = max(1, int(risk_budget / (px * 100)))
        cost = round(contracts * px * 100, 2)
        spread_pct = round(_leg_spread_pct(row), 3)
        wide = spread_pct > WIDE_MARKET_THRESHOLD

        kind = "CALL" if is_call else "PUT"
        note = (f"Buy {strike:g}{kind[0]} @ ~${px:.2f}. Max loss ${px*100:.0f}/contract (the whole "
                f"premium). If {ticker} reaches ${target:.2f} by expiry this is worth roughly "
                f"${intrinsic_at_target*100:.0f}/contract ({upside_pct:+d}%).")
        if wide:
            note += (f" WIDE MARKET ({spread_pct*100:.0f}% bid/ask) - use a limit order.")

        stock_cost = contracts * 100 * entry
        lev = round(stock_cost / cost, 1) if cost > 0 else 0

        return OptionsPlay(
            strategy=f"Long {kind}",
            expiry=expiry, dte=dte,
            legs=[OptionLeg("BUY", kind, strike, round(px, 2))],
            net_debit=round(px, 2),
            max_profit=round(intrinsic_at_target - px, 2),  # at the engine's target
            max_loss=round(px, 2),
            breakeven=breakeven,
            risk_reward=round((intrinsic_at_target - px) / px, 2) if px > 0 else 0,
            prob_profit=pop,
            contracts=contracts, cost=cost,
            capital_vs_stock=f"{lev}x leverage vs ${stock_cost:,.0f} in shares",
            note=note,
            quoted_at=datetime.now().isoformat(timespec="seconds"),
            max_spread_pct=spread_pct, wide_market=wide,
        )
    except Exception:
        return None


def _bull_call_spread(calls, entry, target, dte, expiry, spot, risk_budget):
    calls = calls.sort_values("strike").reset_index(drop=True)
    if len(calls) < 2:
        return None
    long_row = _nearest(calls, entry)               # buy ATM call
    long_k = float(long_row["strike"])
    # Short leg: nearest strike to our profit target, must be above the long strike.
    higher = calls[calls["strike"] > long_k]
    if higher.empty:
        return None
    short_row = _nearest(higher, target)
    short_k = float(short_row["strike"])

    long_px = _leg_price(long_row, "buy")
    short_px = _leg_price(short_row, "sell")
    if long_px <= 0 or short_px <= 0 or short_px >= long_px:
        return None
    net_debit = round(long_px - short_px, 2)
    width = short_k - long_k
    if net_debit <= 0 or net_debit >= width:
        return None

    max_profit = round(width - net_debit, 2)
    breakeven = round(long_k + net_debit, 2)
    rr = round(max_profit / net_debit, 2)
    iv = float(long_row.get("impliedVolatility") or 0)
    pop = int(round(_prob_above(spot, breakeven, iv, dte) * 100))
    contracts = max(1, int(risk_budget / (net_debit * 100)))
    cost = round(contracts * net_debit * 100, 2)
    stock_cost = contracts * 100 * entry
    lev = round(stock_cost / cost, 1) if cost > 0 else 0
    spread_pct = round(max(_leg_spread_pct(long_row), _leg_spread_pct(short_row)), 3)
    wide = spread_pct > WIDE_MARKET_THRESHOLD

    note = f"Buy {long_k:g}C / sell {short_k:g}C. Max loss ${net_debit*100:.0f}/contract, max gain ${max_profit*100:.0f}."
    if wide:
        note += (f" WIDE MARKET ({spread_pct*100:.0f}% bid/ask spread) - "
                 "your real fill will likely be worse than this quote. Use a limit order.")

    return OptionsPlay(
        strategy="Bull Call Spread",
        expiry=expiry, dte=dte,
        legs=[
            OptionLeg("BUY", "CALL", long_k, round(long_px, 2)),
            OptionLeg("SELL", "CALL", short_k, round(short_px, 2)),
        ],
        net_debit=net_debit, max_profit=max_profit, max_loss=net_debit,
        breakeven=breakeven, risk_reward=rr, prob_profit=pop,
        contracts=contracts, cost=cost,
        capital_vs_stock=f"{lev}x leverage vs ${stock_cost:,.0f} in shares",
        note=note,
        quoted_at=datetime.now().isoformat(timespec="seconds"),
        max_spread_pct=spread_pct, wide_market=wide,
    )


def _bear_put_spread(puts, entry, target, dte, expiry, spot, risk_budget):
    puts = puts.sort_values("strike").reset_index(drop=True)
    if len(puts) < 2:
        return None
    long_row = _nearest(puts, entry)                # buy ATM put
    long_k = float(long_row["strike"])
    # Short leg: nearest strike to downside target, must be below the long strike.
    lower = puts[puts["strike"] < long_k]
    if lower.empty:
        return None
    short_row = _nearest(lower, target)
    short_k = float(short_row["strike"])

    long_px = _leg_price(long_row, "buy")
    short_px = _leg_price(short_row, "sell")
    if long_px <= 0 or short_px <= 0 or short_px >= long_px:
        return None
    net_debit = round(long_px - short_px, 2)
    width = long_k - short_k
    if net_debit <= 0 or net_debit >= width:
        return None

    max_profit = round(width - net_debit, 2)
    breakeven = round(long_k - net_debit, 2)
    rr = round(max_profit / net_debit, 2)
    iv = float(long_row.get("impliedVolatility") or 0)
    # profit if price finishes BELOW breakeven
    pop = int(round((1 - _prob_above(spot, breakeven, iv, dte)) * 100))
    contracts = max(1, int(risk_budget / (net_debit * 100)))
    cost = round(contracts * net_debit * 100, 2)
    stock_cost = contracts * 100 * entry
    lev = round(stock_cost / cost, 1) if cost > 0 else 0
    spread_pct = round(max(_leg_spread_pct(long_row), _leg_spread_pct(short_row)), 3)
    wide = spread_pct > WIDE_MARKET_THRESHOLD

    note = f"Buy {long_k:g}P / sell {short_k:g}P. Max loss ${net_debit*100:.0f}/contract, max gain ${max_profit*100:.0f}."
    if wide:
        note += (f" WIDE MARKET ({spread_pct*100:.0f}% bid/ask spread) - "
                 "your real fill will likely be worse than this quote. Use a limit order.")

    return OptionsPlay(
        strategy="Bear Put Spread",
        expiry=expiry, dte=dte,
        legs=[
            OptionLeg("BUY", "PUT", long_k, round(long_px, 2)),
            OptionLeg("SELL", "PUT", short_k, round(short_px, 2)),
        ],
        net_debit=net_debit, max_profit=max_profit, max_loss=net_debit,
        breakeven=breakeven, risk_reward=rr, prob_profit=pop,
        contracts=contracts, cost=cost,
        capital_vs_stock=f"{lev}x leverage vs ${stock_cost:,.0f} in shares",
        note=note,
        quoted_at=datetime.now().isoformat(timespec="seconds"),
        max_spread_pct=spread_pct, wide_market=wide,
    )


def play_to_dict(p: OptionsPlay | None) -> dict | None:
    if p is None:
        return None
    return {
        "strategy": p.strategy,
        "expiry": p.expiry,
        "dte": p.dte,
        "legs": [{"action": l.action, "type": l.type, "strike": l.strike, "price": l.price} for l in p.legs],
        "net_debit": p.net_debit,
        "max_profit": p.max_profit,
        "max_loss": p.max_loss,
        "breakeven": p.breakeven,
        "risk_reward": p.risk_reward,
        "prob_profit": p.prob_profit,
        "contracts": p.contracts,
        "cost": p.cost,
        "capital_vs_stock": p.capital_vs_stock,
        "note": p.note,
        "quoted_at": p.quoted_at,
        "max_spread_pct": p.max_spread_pct,
        "wide_market": p.wide_market,
    }
