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
    # --- trade-quality gates (see signals/context.py for the reasoning) ---
    open_interest: int = 0        # contracts outstanding at the chosen strike
    strike_volume: int = 0        # contracts traded today at that strike
    illiquid: bool = False        # too thin to enter and exit cleanly
    delta: float = 0.0            # option delta (0-1); how much it moves per $1 of stock
    iv_verdict: str = ""          # cheap | fair | rich | very_rich
    iv_ratio: float | None = None # implied vol / realized vol
    iv_expensive: bool = False
    earnings_date: str | None = None
    earnings_in_hold: bool = False
    regime_aligned: bool = True
    against_trend: bool = False
    warnings: list = None         # human-readable reasons to think twice
    quality_score: int = 100      # 0-100 after penalties; low = skip it


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

# A strike with a handful of contracts outstanding has no real market: you are the only
# participant, so you pay up to get in and give it back to get out. Seen live on this
# account - VRSN open interest 3, JEF 1. Those fills are where returns quietly disappear.
MIN_OPEN_INTEREST = 100
MIN_OI_SOFT = 25              # below this it is unusable; between the two it is a warning

# Directional longs want a strike that actually tracks the stock. Deep OTM options are
# lottery tickets (tiny delta, all theta); deep ITM ties up capital for little leverage.
# ~0.55-0.70 delta is the sweet spot: real participation in the move, manageable decay.
TARGET_DELTA = 0.62
DELTA_MIN, DELTA_MAX = 0.45, 0.80

# Minimum credit as a fraction of strike width when SELLING premium. Guards against
# spreads that collect pennies while risking the full width.
MIN_CREDIT_FRAC = 0.12


def _bs_delta(spot: float, strike: float, dte: int, iv: float, kind: str) -> float:
    """Black-Scholes delta - how much the option moves per $1 move in the stock."""
    if dte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        intrinsic = (spot > strike) if kind == "call" else (spot < strike)
        return 1.0 if intrinsic else 0.0
    T = dte / 365.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    nd1 = _norm_cdf(d1)
    return nd1 if kind == "call" else nd1 - 1.0


def _leg_liquidity(row) -> tuple[int, int, bool]:
    """(open_interest, volume, is_illiquid) for a chosen strike."""
    try:
        oi = int(float(row.get("openInterest") or 0))
    except Exception:
        oi = 0
    try:
        v = row.get("volume")
        vol = 0 if v is None or (isinstance(v, float) and math.isnan(v)) else int(float(v))
    except Exception:
        vol = 0
    return oi, vol, oi < MIN_OI_SOFT


def assess_quality(ticker: str, signal: str, expiry: str, iv: float,
                   open_interest: int, wide: bool, spread_pct: float,
                   is_debit_spread: bool = False) -> dict:
    """Score every way a directionally-correct trade can still lose money.

    Shared by BOTH structures. A vertical spread is less exposed to some of these than a
    naked long - the short leg you sell is priced with the same inflated IV you're paying
    on the long leg, so rich premium partly cancels - but it is not immune, and earnings
    or a strike nobody trades will wreck a spread just as thoroughly.
    """
    from .context import iv_assessment, earnings_check, regime_alignment

    warnings: list[str] = []
    score = 100

    ivx = iv_assessment(ticker, iv)
    # A debit spread finances part of its cost by selling premium at the same rich IV,
    # so an expensive-vol regime hurts it roughly half as much as an outright long.
    iv_weight = 0.5 if is_debit_spread else 1.0
    if ivx["verdict"] == "very_rich":
        score -= int(30 * iv_weight)
        warnings.append(f"Very expensive premium ({ivx['ratio']}x realized vol)")
    elif ivx["verdict"] == "rich":
        score -= int(15 * iv_weight)
        warnings.append(f"Expensive premium ({ivx['ratio']}x realized vol)")
    elif ivx["verdict"] == "cheap":
        score += 5

    earn = earnings_check(ticker, expiry)
    if earn["in_hold"]:
        score -= 35
        warnings.append(f"Earnings {earn['date']} inside the hold - IV crush risk")
    elif earn["before_expiry"]:
        score -= 5
        warnings.append(f"Earnings {earn['date']} before expiry - be out first")

    if open_interest < MIN_OI_SOFT:
        score -= 30
        warnings.append(f"Almost no open interest ({open_interest}) - you may not get filled fairly")
    elif open_interest < MIN_OPEN_INTEREST:
        score -= 12
        warnings.append(f"Thin open interest ({open_interest})")

    if wide:
        score -= 12
        warnings.append(f"Wide bid/ask ({spread_pct*100:.0f}%) - use a limit order")

    reg = regime_alignment(signal)
    if reg.get("against_trend"):
        score -= 15
        warnings.append(f"Against the market trend ({reg.get('regime')})")

    return {
        "score": max(0, min(100, score)),
        "warnings": warnings,
        "iv": ivx,
        "earnings": earn,
        "regime": reg,
    }


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
            return _bull_call_spread(chain.calls, entry, target, dte, expiry, entry,
                                     risk_budget, ticker)
        else:
            return _bear_put_spread(chain.puts, entry, target, dte, expiry, entry,
                                    risk_budget, ticker)
    except Exception:
        return None


def _nearest(df, strike_target):
    idx = (df["strike"] - strike_target).abs().idxmin()
    return df.loc[idx]


def build_credit_spread(ticker: str, signal: str, entry: float, target: float,
                        stop: float, risk_budget: float = 200.0,
                        short_delta: float = 0.25) -> "OptionsPlay | None":
    """Defined-risk CREDIT spread - get paid for time passing instead of paying for it.

    Everything else in this module BUYS premium, which needs a real move to pay off. In a
    sideways, low-volatility tape that is the losing side of the trade: theta grinds the
    position down every day while the move never arrives. Backtesting the long strategies
    across a choppy stretch produced a loss in every window.

    A credit spread inverts the bet. You SELL an out-of-the-money option and BUY a further
    one as insurance, collecting the difference up front. You keep it if the stock simply
    fails to travel far enough against you - which is exactly what chop does.

      BUY  signal (bullish/neutral) -> Bull Put Spread  (sell put below, buy lower put)
      SELL signal (bearish/neutral) -> Bear Call Spread (sell call above, buy higher call)

    The trade-off is honest and must be respected: these win often and lose big. Max profit
    is the credit; max loss is the strike width minus that credit, typically several times
    larger. The long wing is what makes the loss finite - this is never a naked short.

    `short_delta` sets how far out-of-the-money the sold strike sits. ~0.25 delta means
    roughly a 75% chance of expiring worthless (your win), which is the conventional
    premium-selling zone.
    """
    if signal not in ("BUY", "SELL"):
        return None
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None
        expiry, dte = _pick_expiry(exps, min_dte=20, max_dte=45)
        if not expiry:
            return None
        chain = t.option_chain(expiry)
        # Use the LIVE spot, not the caller's entry price. Strike selection and the
        # probability math must agree with the same prices the chain is quoting; a stale
        # or mismatched entry produces nonsense like a credit larger than the max loss.
        try:
            entry = float(t.history(period="1d")["Close"].iloc[-1]) or entry
        except Exception:
            pass
    except Exception:
        return None

    try:
        is_bullish = signal == "BUY"
        kind_l = "put" if is_bullish else "call"
        df = (chain.puts if is_bullish else chain.calls).sort_values("strike").reset_index(drop=True)
        if len(df) < 3:
            return None

        # Find the strike closest to the target short delta, on the correct side of spot.
        cands = []
        for _, r in df.iterrows():
            k = float(r["strike"])
            riv = float(r.get("impliedVolatility") or 0)
            if riv <= 0 or k <= 0:
                continue
            if is_bullish and k >= entry:      # puts must be below spot
                continue
            if not is_bullish and k <= entry:  # calls must be above spot
                continue
            d = abs(_bs_delta(entry, k, dte, riv, kind_l))
            cands.append((abs(d - short_delta), k, r, d, riv))
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        _, short_k, short_row, sdelta, iv = cands[0]

        # Choose the protective wing by searching, not by assuming. A wider wing collects
        # slightly more premium but risks far more, so the ratio that matters -
        # credit relative to width - actually gets WORSE as the wing moves out. Try the
        # nearest few strikes and keep the best-paying one.
        inc = strike_increment(entry)
        best = None
        for mult in (1, 2, 3):
            wing = short_k - inc * mult if is_bullish else short_k + inc * mult
            lrow = _nearest(df, wing)
            lk = float(lrow["strike"])
            if (is_bullish and lk >= short_k) or (not is_bullish and lk <= short_k):
                continue
            w = abs(short_k - lk)
            if w <= 0:
                continue
            cr = _leg_price(short_row, "sell") - _leg_price(lrow, "buy")
            if cr <= 0.02 or cr >= w:
                continue
            frac = cr / w
            if best is None or frac > best[0]:
                best = (frac, cr, w, lk, lrow)

        # The credit must be a meaningful share of what's being risked. Collecting $4
        # against a $1,096 max loss is a 274:1 bet against you - one loss erases 274 wins.
        if best is None or best[0] < MIN_CREDIT_FRAC:
            return None
        _, credit, width, long_k, long_row = best

        max_loss = width - credit
        breakeven = (short_k - credit) if is_bullish else (short_k + credit)
        # Probability the short strike expires worthless (i.e. you keep the credit).
        p_above = _prob_above(entry, breakeven, iv, dte)
        pop = int(round((p_above if is_bullish else 1 - p_above) * 100))

        oi_s, vol_s, _ = _leg_liquidity(short_row)
        oi_l, _, _ = _leg_liquidity(long_row)
        oi = min(oi_s, oi_l)
        spread_pct = round(max(_leg_spread_pct(short_row), _leg_spread_pct(long_row)), 3)
        wide = spread_pct > WIDE_MARKET_THRESHOLD

        # Risk here is max_loss per contract, not the premium.
        contracts = max(1, int(risk_budget / (max_loss * 100))) if max_loss > 0 else 1
        collateral = round(max_loss * 100 * contracts, 2)

        qa = assess_quality(ticker, signal, expiry, iv, oi, wide, spread_pct,
                            is_debit_spread=False)
        # Rich IV is a REASON to sell, not a penalty - undo the buyer's-side deduction.
        warnings = [w for w in qa["warnings"] if "expensive premium" not in w.lower()]
        score = qa["score"]
        ivx = qa["iv"]
        if ivx["verdict"] in ("rich", "very_rich"):
            score = min(100, score + (30 if ivx["verdict"] == "very_rich" else 15) + 5)
            warnings.append(f"Rich premium ({ivx['ratio']}x realized) - good for SELLING")
        elif ivx["verdict"] == "cheap":
            score -= 10
            warnings.append("Cheap premium - little to collect selling here")
        score = max(0, min(100, score))

        strategy = "Bull Put Spread" if is_bullish else "Bear Call Spread"
        kind_u = "PUT" if is_bullish else "CALL"
        note = (f"Sell {short_k:g}{kind_u[0]} / buy {long_k:g}{kind_u[0]} for a "
                f"${credit*100:.0f} credit per contract. You keep it if {ticker} stays "
                f"{'above' if is_bullish else 'below'} ${breakeven:.2f}. Max loss "
                f"${max_loss*100:.0f}/contract. Wins often, loses big - respect the size.")
        if warnings:
            note += " " + "; ".join(warnings) + "."

        return OptionsPlay(
            strategy=strategy, expiry=expiry, dte=dte,
            legs=[
                OptionLeg("SELL", kind_u, short_k, round(_leg_price(short_row, "sell"), 2)),
                OptionLeg("BUY", kind_u, long_k, round(_leg_price(long_row, "buy"), 2)),
            ],
            net_debit=round(-credit, 2),        # negative = you receive money
            max_profit=round(credit, 2),
            max_loss=round(max_loss, 2),
            breakeven=round(breakeven, 2),
            risk_reward=round(credit / max_loss, 2) if max_loss > 0 else 0,
            prob_profit=pop, contracts=contracts, cost=collateral,
            capital_vs_stock=f"${collateral:,.0f} collateral held against max loss",
            note=note,
            quoted_at=datetime.now().isoformat(timespec="seconds"),
            max_spread_pct=spread_pct, wide_market=wide,
            open_interest=oi, strike_volume=vol_s, illiquid=oi < MIN_OI_SOFT,
            delta=round(sdelta, 3),
            iv_verdict=ivx["verdict"], iv_ratio=ivx["ratio"],
            iv_expensive=bool(ivx["expensive"]),
            earnings_date=qa["earnings"]["date"],
            earnings_in_hold=bool(qa["earnings"]["in_hold"]),
            regime_aligned=bool(qa["regime"].get("aligned", True)),
            against_trend=bool(qa["regime"].get("against_trend", False)),
            warnings=warnings, quality_score=score,
        )
    except Exception:
        return None


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

        kind_l = "call" if is_call else "put"

        # --- Strike selection by DELTA, not by raw moneyness ---------------------
        # Pick the strike whose delta is closest to TARGET_DELTA, restricted to a sane
        # band, and prefer liquid strikes when deltas are close. Falls back to the old
        # at-the-money pick if the chain has no usable implied vols.
        cands = []
        for _, r in df.iterrows():
            k = float(r["strike"])
            riv = float(r.get("impliedVolatility") or 0)
            if riv <= 0 or k <= 0:
                continue
            d = abs(_bs_delta(entry, k, dte, riv, kind_l))
            if DELTA_MIN <= d <= DELTA_MAX:
                oi, vol, thin = _leg_liquidity(r)
                cands.append((abs(d - TARGET_DELTA), thin, -oi, k, r, d))
        if cands:
            # closest delta first; among near-ties prefer the liquid one
            cands.sort(key=lambda c: (round(c[0], 2), c[1], c[2]))
            _, _, _, strike, row, delta = cands[0]
        else:
            want = entry * moneyness if is_call else entry * (2 - moneyness)
            row = _nearest(df, want)
            strike = float(row["strike"])
            delta = abs(_bs_delta(entry, strike, dte,
                                  float(row.get("impliedVolatility") or 0), kind_l))

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
        oi, strike_vol, thin = _leg_liquidity(row)

        qa = assess_quality(ticker, signal, expiry, iv, oi, wide, spread_pct,
                            is_debit_spread=False)
        score, warnings = qa["score"], qa["warnings"]
        ivx, earn, reg = qa["iv"], qa["earnings"], qa["regime"]

        note = (f"Buy {strike:g}{kind[0]} @ ~${px:.2f} (delta {delta:.2f}). Max loss "
                f"${px*100:.0f}/contract (the whole premium). If {ticker} reaches "
                f"${target:.2f} by expiry this is worth roughly "
                f"${intrinsic_at_target*100:.0f}/contract ({upside_pct:+d}%).")
        if warnings:
            note += " ⚠ " + "; ".join(warnings) + "."

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
            open_interest=oi, strike_volume=strike_vol, illiquid=thin,
            delta=round(delta, 3),
            iv_verdict=ivx["verdict"], iv_ratio=ivx["ratio"],
            iv_expensive=bool(ivx["expensive"]),
            earnings_date=earn["date"], earnings_in_hold=bool(earn["in_hold"]),
            regime_aligned=bool(reg.get("aligned", True)),
            against_trend=bool(reg.get("against_trend", False)),
            warnings=warnings, quality_score=score,
        )
    except Exception:
        return None


def _bull_call_spread(calls, entry, target, dte, expiry, spot, risk_budget, ticker=""):
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
    # Liquidity of a spread is only as good as its worst leg - you have to fill both.
    oi_long, _, _ = _leg_liquidity(long_row)
    oi_short, _, _ = _leg_liquidity(short_row)
    oi = min(oi_long, oi_short)
    delta = abs(_bs_delta(spot, long_k, dte, iv, "call"))

    qa = assess_quality(ticker, "BUY", expiry, iv, oi, wide, spread_pct,
                        is_debit_spread=True)
    ivx, earn, reg = qa["iv"], qa["earnings"], qa["regime"]

    note = f"Buy {long_k:g}C / sell {short_k:g}C. Max loss ${net_debit*100:.0f}/contract, max gain ${max_profit*100:.0f}."
    if qa["warnings"]:
        note += " ⚠ " + "; ".join(qa["warnings"]) + "."

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
        open_interest=oi, illiquid=oi < MIN_OI_SOFT, delta=round(delta, 3),
        iv_verdict=ivx["verdict"], iv_ratio=ivx["ratio"],
        iv_expensive=bool(ivx["expensive"]),
        earnings_date=earn["date"], earnings_in_hold=bool(earn["in_hold"]),
        regime_aligned=bool(reg.get("aligned", True)),
        against_trend=bool(reg.get("against_trend", False)),
        warnings=qa["warnings"], quality_score=qa["score"],
    )


def _bear_put_spread(puts, entry, target, dte, expiry, spot, risk_budget, ticker=""):
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
    oi_long, _, _ = _leg_liquidity(long_row)
    oi_short, _, _ = _leg_liquidity(short_row)
    oi = min(oi_long, oi_short)   # you must fill both legs
    delta = abs(_bs_delta(spot, long_k, dte, iv, "put"))

    qa = assess_quality(ticker, "SELL", expiry, iv, oi, wide, spread_pct,
                        is_debit_spread=True)
    ivx, earn, reg = qa["iv"], qa["earnings"], qa["regime"]

    note = f"Buy {long_k:g}P / sell {short_k:g}P. Max loss ${net_debit*100:.0f}/contract, max gain ${max_profit*100:.0f}."
    if qa["warnings"]:
        note += " ⚠ " + "; ".join(qa["warnings"]) + "."

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
        open_interest=oi, illiquid=oi < MIN_OI_SOFT, delta=round(delta, 3),
        iv_verdict=ivx["verdict"], iv_ratio=ivx["ratio"],
        iv_expensive=bool(ivx["expensive"]),
        earnings_date=earn["date"], earnings_in_hold=bool(earn["in_hold"]),
        regime_aligned=bool(reg.get("aligned", True)),
        against_trend=bool(reg.get("against_trend", False)),
        warnings=qa["warnings"], quality_score=qa["score"],
    )


def limit_price_guidance(net_price: float, spread_pct: float, is_credit: bool = False) -> dict:
    """Turn "use a limit order" into an actual number to type into the broker.

    Saying "use a limit" without a price is useless advice. On a wide market the
    difference between paying the ask and working an order near mid is several percent
    of the trade - which, per the walk-forward, is roughly the size of the entire edge.
    """
    px = abs(net_price)
    half = px * spread_pct / 2 if spread_pct else px * 0.02
    if is_credit:
        # Selling: start greedy (above mid), settle no lower than a bit under mid.
        start = round(px + half * 0.4, 2)
        walk = round(px, 2)
        floor = round(px - half * 0.6, 2)
        return {"start": start, "fair": walk, "worst_acceptable": floor,
                "instruction": (f"Place a LIMIT SELL to open at ${start:.2f} credit. "
                                f"If unfilled in a few minutes, work down toward ${walk:.2f}. "
                                f"Do not accept less than ${floor:.2f} - below that the "
                                "premium stops paying for the risk.")}
    start = round(px - half * 0.4, 2)
    walk = round(px, 2)
    cap = round(px + half * 0.6, 2)
    return {"start": start, "fair": walk, "worst_acceptable": cap,
            "instruction": (f"Place a LIMIT BUY at ${start:.2f}. If unfilled in a few "
                            f"minutes, raise toward ${walk:.2f}. Never chase past "
                            f"${cap:.2f} - paying up erases the edge on a trade this size.")}


def play_to_dict(p: OptionsPlay | None) -> dict | None:
    if p is None:
        return None
    _is_credit = p.net_debit < 0
    return {
        "limit_guidance": limit_price_guidance(
            p.net_debit if not _is_credit else p.max_profit,
            p.max_spread_pct, is_credit=_is_credit),
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
        "open_interest": p.open_interest,
        "strike_volume": p.strike_volume,
        "illiquid": p.illiquid,
        "delta": p.delta,
        "iv_verdict": p.iv_verdict,
        "iv_ratio": p.iv_ratio,
        "iv_expensive": p.iv_expensive,
        "earnings_date": p.earnings_date,
        "earnings_in_hold": p.earnings_in_hold,
        "regime_aligned": p.regime_aligned,
        "against_trend": p.against_trend,
        "warnings": p.warnings or [],
        "quality_score": p.quality_score,
    }
