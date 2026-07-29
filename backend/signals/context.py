"""
Trade context: the checks that decide whether a *correct* directional call is also a
*profitable* trade. Everything here is about avoiding bad entries rather than finding
more of them - which, on a small long-options account, is where the money actually is.

Four things this answers:

  1. AM I OVERPAYING?  (iv_assessment)
     A long option's cost is implied volatility. If IV is far above what the stock
     actually moves (realized vol), you need a bigger move just to break even. Buying
     the same direction at IV/RV 0.9 instead of 1.3 is free money over many trades.

  2. IS THERE AN EARNINGS LANDMINE?  (earnings_check)
     Options carry an IV premium into earnings that collapses the moment the number
     prints - "IV crush". A call can lose value on an earnings beat. Holding a long
     option through earnings is a coin flip with the odds shaved against you.

  3. CAN I ACTUALLY GET OUT?  (liquidity handled in options.py)
     A strike with 3 contracts of open interest has no real market.

  4. AM I FIGHTING THE TAPE?  (market_regime)
     Most stocks follow the index. Buying calls in a confirmed downtrend is choosing
     the hardest version of the trade.

All lookups are cached - these run per-candidate during plan building, not per-ticker
across the whole 900-name scan.
"""

from datetime import datetime, date, timedelta
import time
import numpy as np
import yfinance as yf

_cache: dict = {}
_TTL = 6 * 3600  # earnings/sector move rarely; 6h is plenty


def _cached(key: str, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit["ts"] < _TTL:
        return hit["val"]
    val = fn()
    _cache[key] = {"val": val, "ts": now}
    return val


# ---------------------------------------------------------------- earnings

def get_earnings_date(ticker: str) -> date | None:
    """Next scheduled earnings date, or None if unknown."""
    def fetch():
        try:
            cal = yf.Ticker(ticker).calendar
            if not isinstance(cal, dict):
                return None
            ed = cal.get("Earnings Date")
            if isinstance(ed, list) and ed:
                ed = ed[0]
            if isinstance(ed, datetime):
                return ed.date()
            if isinstance(ed, date):
                return ed
        except Exception:
            pass
        return None
    return _cached(f"earn:{ticker}", fetch)


def earnings_check(ticker: str, expiry: str, hold_days: int = 10) -> dict:
    """Does an earnings event fall inside the option's life or the intended hold?

    `blocking` is reserved for the genuinely dangerous case: earnings landing inside the
    days you actually plan to hold. Earnings later in the contract's life is a warning,
    not a veto, because the exit ladder will normally be out well before then.
    """
    ed = get_earnings_date(ticker)
    if ed is None:
        return {"date": None, "days_away": None, "in_hold": False,
                "before_expiry": False, "blocking": False, "note": ""}

    today = date.today()
    days_away = (ed - today).days
    try:
        exp = datetime.strptime(expiry, "%Y-%m-%d").date()
    except Exception:
        exp = today

    before_expiry = today <= ed <= exp
    in_hold = 0 <= days_away <= hold_days

    note = ""
    if in_hold:
        note = (f"Earnings {ed:%b %d} ({days_away}d) lands inside the intended hold - "
                "IV crush can sink this even if the direction is right.")
    elif before_expiry:
        note = (f"Earnings {ed:%b %d} ({days_away}d) falls before expiry - "
                "plan to be out before then.")

    return {"date": ed.isoformat(), "days_away": days_away, "in_hold": in_hold,
            "before_expiry": before_expiry, "blocking": in_hold, "note": note}


# ---------------------------------------------------------------- volatility

def realized_vol(ticker: str, window: int = 20, period: str = "1y") -> float:
    def fetch():
        try:
            h = yf.Ticker(ticker).history(period=period)
            if h is None or h.empty or len(h) < window + 5:
                return 0.0
            r = np.log(h["Close"] / h["Close"].shift(1)).dropna()
            return float(r.tail(window).std() * np.sqrt(252))
        except Exception:
            return 0.0
    return _cached(f"rv:{ticker}:{window}", fetch)


def vol_percentile(ticker: str) -> float | None:
    """Where today's 20d realized vol sits within the last year of 20d readings (0-100).

    True IV rank needs a year of historical implied vol, which the free feed does not
    provide. Realized-vol percentile is the honest available proxy for "is this name
    unusually jumpy right now".
    """
    def fetch():
        try:
            h = yf.Ticker(ticker).history(period="1y")
            if h is None or h.empty or len(h) < 60:
                return None
            r = np.log(h["Close"] / h["Close"].shift(1)).dropna()
            rolling = r.rolling(20).std() * np.sqrt(252)
            rolling = rolling.dropna()
            if len(rolling) < 30:
                return None
            cur = float(rolling.iloc[-1])
            return round(float((rolling < cur).mean() * 100), 1)
        except Exception:
            return None
    return _cached(f"vpct:{ticker}", fetch)


# How far implied can sit above realized before the premium stops being worth paying.
IV_RICH = 1.25      # paying >=25% over what the stock actually moves
IV_VERY_RICH = 1.45


def iv_assessment(ticker: str, implied_vol: float) -> dict:
    """Compare the IV you're being charged against the stock's actual movement."""
    rv = realized_vol(ticker)
    if not implied_vol or implied_vol <= 0 or rv <= 0:
        return {"iv": round(implied_vol or 0, 4), "rv": round(rv, 4), "ratio": None,
                "vol_pct": vol_percentile(ticker), "verdict": "unknown",
                "expensive": False, "note": ""}

    ratio = implied_vol / rv
    if ratio >= IV_VERY_RICH:
        verdict, expensive = "very_rich", True
        note = (f"IV {implied_vol*100:.0f}% vs realized {rv*100:.0f}% ({ratio:.2f}x) - "
                "you are paying a steep premium over how much this stock actually moves.")
    elif ratio >= IV_RICH:
        verdict, expensive = "rich", True
        note = (f"IV {implied_vol*100:.0f}% vs realized {rv*100:.0f}% ({ratio:.2f}x) - "
                "premium is expensive relative to actual movement.")
    elif ratio <= 0.90:
        verdict, expensive = "cheap", False
        note = (f"IV {implied_vol*100:.0f}% vs realized {rv*100:.0f}% ({ratio:.2f}x) - "
                "premium is cheap relative to how much this stock moves.")
    else:
        verdict, expensive = "fair", False
        note = f"IV {implied_vol*100:.0f}% vs realized {rv*100:.0f}% ({ratio:.2f}x) - fairly priced."

    return {"iv": round(implied_vol, 4), "rv": round(rv, 4), "ratio": round(ratio, 2),
            "vol_pct": vol_percentile(ticker), "verdict": verdict,
            "expensive": expensive, "note": note}


# ---------------------------------------------------------------- market regime

def market_regime() -> dict:
    """Index trend from SPY. Buying calls into a confirmed downtrend is the hard trade."""
    def fetch():
        try:
            h = yf.Ticker("SPY").history(period="1y")
            if h is None or h.empty or len(h) < 210:
                return {"regime": "unknown", "note": "", "bias": 0.0,
                        "spy": None, "ema50": None, "ema200": None}
            c = h["Close"]
            e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
            e200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1])
            px = float(c.iloc[-1])

            if px > e50 > e200:
                regime, bias = "bull", 1.0
                note = "SPY above its 50 and 200 EMA - broad uptrend, favour calls."
            elif px < e50 < e200:
                regime, bias = "bear", -1.0
                note = "SPY below its 50 and 200 EMA - broad downtrend, favour puts."
            elif px > e200:
                regime, bias = "choppy_bull", 0.4
                note = "SPY above its 200 EMA but not cleanly trending - mixed, size smaller."
            else:
                regime, bias = "choppy_bear", -0.4
                note = "SPY below its 200 EMA and not cleanly trending - mixed, size smaller."

            return {"regime": regime, "bias": bias, "note": note,
                    "spy": round(px, 2), "ema50": round(e50, 2), "ema200": round(e200, 2)}
        except Exception:
            return {"regime": "unknown", "note": "", "bias": 0.0,
                    "spy": None, "ema50": None, "ema200": None}

    # Shorter TTL: regime is a live read, not a static attribute.
    now = time.time()
    hit = _cache.get("regime")
    if hit and now - hit["ts"] < 1800:
        return hit["val"]
    val = fetch()
    _cache["regime"] = {"val": val, "ts": now}
    return val


def macro_from_market() -> tuple[float, str]:
    """Macro score (-2..+2) derived from PRICE, not headlines.

    The news-based macro score was permanently 0.0 because it needs a paid news API key,
    leaving a whole scoring input inert. Volatility and index trend are free, always
    available, and arguably more honest: VIX is the market pricing its own fear in real
    money rather than a model's reading of journalism.

    Inputs: VIX level, VIX 5-day direction, and SPY's position relative to its 50/200 EMA.
    """
    def fetch():
        score = 0.0
        bits: list[str] = []
        try:
            vix = yf.Ticker("^VIX").history(period="3mo")
            if vix is not None and not vix.empty:
                v = float(vix["Close"].iloc[-1])
                v5 = float(vix["Close"].iloc[-6]) if len(vix) > 6 else v
                if v < 15:
                    score += 1.0
                    bits.append(f"VIX {v:.1f} (calm)")
                elif v < 20:
                    score += 0.3
                    bits.append(f"VIX {v:.1f} (normal)")
                elif v < 28:
                    score -= 0.7
                    bits.append(f"VIX {v:.1f} (elevated)")
                else:
                    score -= 1.5
                    bits.append(f"VIX {v:.1f} (fear)")
                chg = (v - v5) / v5 * 100 if v5 else 0
                if chg > 15:
                    score -= 0.5
                    bits.append(f"VIX +{chg:.0f}% in a week (risk-off)")
                elif chg < -15:
                    score += 0.4
                    bits.append(f"VIX {chg:.0f}% in a week (risk-on)")
        except Exception:
            pass

        r = market_regime()
        score += r.get("bias", 0.0) * 0.8
        if r.get("regime") != "unknown":
            bits.append(r["regime"].replace("_", " "))

        score = max(-2.0, min(2.0, score))
        label = ("risk-on" if score >= 0.8 else "mildly positive" if score > 0.2
                 else "neutral" if score > -0.2 else
                 "cautious" if score > -0.8 else "risk-off")
        return round(score, 2), f"{label.capitalize()} — {', '.join(bits)}." if bits else label

    now = time.time()
    hit = _cache.get("macro_mkt")
    if hit and now - hit["ts"] < 1800:
        return hit["val"]
    val = fetch()
    _cache["macro_mkt"] = {"val": val, "ts": now}
    return val


def premium_buying_environment() -> dict:
    """Is this a market where BUYING directional options can work at all?

    This exists because backtesting the strategy across five separate windows in a
    choppy, low-volatility, slightly-down tape produced a loss in every single one -
    with both long options and spreads. That is not a bug in the signals; it is what
    buying premium does when the index grinds sideways at 14% realized vol. Every day
    you hold, theta takes a bite, and the move you paid for never arrives.

    Long options need one of two things: an index that TRENDS (so directional bets
    follow through) or volatility that EXPANDS after entry (so premium reprices up).
    Sideways chop at low vol is the one environment that reliably bleeds them.

    Returns a favourability read so the app can tell you to sit out rather than
    quietly feeding a losing setup.
    """
    def fetch():
        try:
            spy = yf.Ticker("SPY").history(period="6mo")
            if spy is None or spy.empty or len(spy) < 60:
                return {"favourable": True, "score": 0, "note": "", "trend_pct": None,
                        "realized_vol": None, "chop": None}

            c = spy["Close"]
            r = np.log(c / c.shift(1)).dropna()
            rv20 = float(r.tail(20).std() * np.sqrt(252))
            trend_20 = float((c.iloc[-1] / c.iloc[-21] - 1) * 100) if len(c) > 21 else 0.0

            # Chop measure: net movement vs total distance travelled. Near 0 = pure
            # round-tripping (bad for directional premium); near 1 = clean trend.
            window = c.tail(21)
            path = float(window.diff().abs().sum())
            net = float(abs(window.iloc[-1] - window.iloc[0]))
            efficiency = (net / path) if path > 0 else 0.0

            score = 0
            bits = []
            if abs(trend_20) >= 3:
                score += 1
                bits.append(f"index trending {trend_20:+.1f}% over a month")
            else:
                score -= 1
                bits.append(f"index going nowhere ({trend_20:+.1f}% in a month)")

            if efficiency >= 0.35:
                score += 1
                bits.append(f"clean directional movement (efficiency {efficiency:.2f})")
            else:
                score -= 1
                bits.append(f"choppy, round-tripping price action (efficiency {efficiency:.2f})")

            if rv20 >= 0.18:
                score += 1
                bits.append(f"volatility {rv20*100:.0f}% gives moves room")
            elif rv20 < 0.13:
                score -= 1
                bits.append(f"volatility only {rv20*100:.0f}% - small moves, theta wins")

            favourable = score >= 0
            headline = ("Reasonable conditions for buying options."
                        if favourable else
                        "POOR conditions for buying options - consider sitting out or "
                        "trading much smaller.")
            return {"favourable": favourable, "score": score,
                    "note": f"{headline} " + "; ".join(bits) + ".",
                    "trend_pct": round(trend_20, 2),
                    "realized_vol": round(rv20 * 100, 1),
                    "chop": round(efficiency, 2)}
        except Exception:
            return {"favourable": True, "score": 0, "note": "", "trend_pct": None,
                    "realized_vol": None, "chop": None}

    now = time.time()
    hit = _cache.get("premium_env")
    if hit and now - hit["ts"] < 3600:
        return hit["val"]
    val = fetch()
    _cache["premium_env"] = {"val": val, "ts": now}
    return val


def recommended_structure() -> dict:
    """Which options structure suits today's market - the answer to "app says don't trade".

    Buying premium and selling premium are opposite bets on the same variable. Long options
    need movement; short options need the absence of it. A tool that only knows how to buy
    has nothing to offer in the sideways tape that dominates most of the year, which is
    exactly the condition the walk-forward backtest showed bleeding money.

    Returns the structure to favour, so the planner can switch sides rather than sit out.
    """
    env = premium_buying_environment()
    reg = market_regime()

    if env["favourable"]:
        rec = "single"
        why = ("Conditions support BUYING premium: there is enough trend and volatility "
               "for a directional move to pay for the time decay.")
    else:
        rec = "credit"
        why = ("Conditions favour SELLING premium. The index is choppy and volatility is "
               "low, which is what grinds long options down. Credit spreads get paid for "
               "that same stillness - they win when the stock simply fails to travel far.")

    return {
        "recommended": rec,
        "why": why,
        "environment_favourable": env["favourable"],
        "environment_note": env["note"],
        "regime": reg.get("regime"),
        "alternatives": {
            "single": "Long call/put - needs a real move; whole premium at risk.",
            "spread": "Debit spread - cheaper directional bet, capped both ways.",
            "credit": "Credit spread - collect premium, win if the stock stays put; "
                      "wins often, loses bigger, always defined-risk.",
        },
    }


def regime_alignment(signal: str) -> dict:
    """Is this trade with or against the index trend?"""
    r = market_regime()
    bias = r.get("bias", 0.0)
    if signal == "BUY":
        aligned = bias > 0
        against = bias <= -0.9
    elif signal == "SELL":
        aligned = bias < 0
        against = bias >= 0.9
    else:
        aligned, against = True, False
    return {**r, "aligned": aligned, "against_trend": against}


# ---------------------------------------------------------------- sector

def get_sector(ticker: str) -> str:
    def fetch():
        try:
            return yf.Ticker(ticker).info.get("sector") or "Unknown"
        except Exception:
            return "Unknown"
    return _cached(f"sec:{ticker}", fetch)
