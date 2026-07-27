"""
Short-term momentum scanner (1-3 day holds) built on intraday bars.

WHAT THIS IS - and honestly, what it is not.

It is NOT a real-time day-trading engine. Two hard reasons, both worth knowing before
risking money:

  1. DATA DELAY. The free Yahoo feed is delayed ~15 minutes intraday. Scalping a 15-minute
     old quote means every fill is against traders who can see the current one. For
     holds measured in minutes that is fatal; for holds measured in days it is noise.

  2. THE PDT RULE. A US margin account under $25,000 equity is limited to 3 day trades
     (open and close the same position, same session) in any rolling 5 business days.
     A 4th trips the pattern-day-trader flag and the account gets restricted. On a
     ~$780 account, true day trading is not a strategy you can legally repeat.

So this module targets the thing that IS available and profitable at this account size:
CLOSE-TO-CLOSE MOMENTUM over 1-3 sessions. It reads the intraday tape to find stocks
whose last session closed strong on real participation - the setups most likely to
follow through the next morning - and sizes stops tighter than the swing engine because
the intended hold is shorter.

Signals are generated from the last COMPLETED session, and acted on at the next open.
"""

from dataclasses import dataclass
import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class MomentumSignal:
    ticker: str
    signal: str            # BUY | SELL | WATCH
    score: float           # -5..+5
    confidence: int        # 0-100
    entry: float
    stop_loss: float
    target: float
    risk_reward: float
    close_range_pct: float # where in the day's range it closed (100 = at the high)
    rel_volume: float      # session volume vs 20-session average
    vwap_dist_pct: float   # close vs session VWAP
    gap_pct: float         # open vs prior close
    atr_pct: float
    hold: str
    reasons: list[str]


def _session_frame(df15: pd.DataFrame) -> pd.DataFrame:
    """Collapse 15-minute bars into per-session OHLCV + VWAP."""
    d = df15.copy()
    d["session"] = d.index.date
    agg = d.groupby("session").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    )
    # Volume-weighted average price per session from the intraday bars.
    d["tp_vol"] = ((d["high"] + d["low"] + d["close"]) / 3) * d["volume"]
    vw = d.groupby("session").agg(tpv=("tp_vol", "sum"), vol=("volume", "sum"))
    agg["vwap"] = (vw["tpv"] / vw["vol"].replace(0, np.nan)).values
    return agg.dropna()


def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(com=n - 1, adjust=False).mean()


def score_momentum(ticker: str, df15: pd.DataFrame | None = None) -> MomentumSignal | None:
    """Score a ticker's short-term follow-through potential from its intraday tape."""
    if df15 is None:
        try:
            df15 = yf.download(ticker, period="1mo", interval="15m",
                               progress=False, auto_adjust=True, threads=False)
        except Exception:
            return None
    if df15 is None or df15.empty or len(df15) < 100:
        return None

    df15 = df15.copy()
    df15.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df15.columns]

    s = _session_frame(df15)
    if len(s) < 12:
        return None

    cur = s.iloc[-1]
    prev = s.iloc[-2]

    rng = float(cur["high"] - cur["low"])
    if rng <= 0:
        return None

    close_range_pct = float((cur["close"] - cur["low"]) / rng * 100)
    avg_vol = float(s["volume"].tail(20).mean())
    rel_volume = float(cur["volume"] / avg_vol) if avg_vol > 0 else 1.0
    vwap_dist_pct = float((cur["close"] - cur["vwap"]) / cur["vwap"] * 100)
    gap_pct = float((cur["open"] - prev["close"]) / prev["close"] * 100)
    day_chg_pct = float((cur["close"] - prev["close"]) / prev["close"] * 100)

    atr = float(_atr(s["high"], s["low"], s["close"]).iloc[-1])
    close = float(cur["close"])
    atr_pct = atr / close * 100 if close else 0

    score = 0.0
    reasons: list[str] = []

    # 1. Closing location - the single most predictive short-term tell. Closing at the
    #    high means buyers held control into the bell; that pressure often carries over.
    if close_range_pct >= 80:
        score += 1.5
        reasons.append(f"Closed at {close_range_pct:.0f}% of the day's range - buyers in control into the bell")
    elif close_range_pct >= 65:
        score += 0.8
        reasons.append(f"Closed strong ({close_range_pct:.0f}% of range)")
    elif close_range_pct <= 20:
        score -= 1.5
        reasons.append(f"Closed at {close_range_pct:.0f}% of range - sellers in control into the bell")
    elif close_range_pct <= 35:
        score -= 0.8
        reasons.append(f"Closed weak ({close_range_pct:.0f}% of range)")

    # 2. Participation. A move without volume is noise; conviction needs bodies behind it.
    if rel_volume >= 2.0:
        score += 1.2 if day_chg_pct > 0 else -1.2
        reasons.append(f"{rel_volume:.1f}x average volume - heavy participation")
    elif rel_volume >= 1.4:
        score += 0.7 if day_chg_pct > 0 else -0.7
        reasons.append(f"{rel_volume:.1f}x average volume")
    elif rel_volume < 0.6:
        score *= 0.6
        reasons.append(f"Only {rel_volume:.1f}x volume - weak conviction, score damped")

    # 3. VWAP. Institutions benchmark fills to VWAP; closing above it means the average
    #    buyer today is in profit, which tends to invite continuation.
    if vwap_dist_pct > 0.5:
        score += 0.8
        reasons.append(f"Closed {vwap_dist_pct:+.1f}% above VWAP")
    elif vwap_dist_pct < -0.5:
        score -= 0.8
        reasons.append(f"Closed {vwap_dist_pct:+.1f}% below VWAP")

    # 4. Follow-through vs exhaustion. A big gap that then faded is a trap, not momentum.
    if gap_pct > 1.5 and close_range_pct < 40:
        score -= 1.2
        reasons.append(f"Gapped +{gap_pct:.1f}% then faded - exhaustion, not momentum")
    elif gap_pct > 1.0 and close_range_pct > 70:
        score += 0.8
        reasons.append(f"Gapped +{gap_pct:.1f}% and held the gains")

    # 5. Multi-session thrust
    three = float((cur["close"] - s.iloc[-4]["close"]) / s.iloc[-4]["close"] * 100) if len(s) >= 4 else 0
    if three > 4:
        score += 0.5
        reasons.append(f"Up {three:.1f}% over 3 sessions")
    elif three < -4:
        score -= 0.5
        reasons.append(f"Down {three:.1f}% over 3 sessions")

    score = max(-5.0, min(5.0, score))
    if score >= 2.0:
        signal = "BUY"
    elif score <= -2.0:
        signal = "SELL"
    else:
        signal = "WATCH"

    # Short holds need tighter risk than the swing engine's 1.5x ATR: the edge decays
    # within days, so a wide stop just guarantees a bigger loss when it fails.
    entry = round(close, 2)
    if signal == "SELL":
        stop = round(entry + atr * 1.0, 2)
        target = round(entry - atr * 2.0, 2)
    else:
        stop = round(entry - atr * 1.0, 2)
        target = round(entry + atr * 2.0, 2)

    risk = abs(entry - stop)
    rr = round(abs(target - entry) / risk, 2) if risk > 0 else 0
    confidence = int(min(100, abs(score) / 5 * 100))

    return MomentumSignal(
        ticker=ticker, signal=signal, score=round(score, 2), confidence=confidence,
        entry=entry, stop_loss=stop, target=target, risk_reward=rr,
        close_range_pct=round(close_range_pct, 1), rel_volume=round(rel_volume, 2),
        vwap_dist_pct=round(vwap_dist_pct, 2), gap_pct=round(gap_pct, 2),
        atr_pct=round(atr_pct, 2),
        hold="1-3 days", reasons=reasons,
    )


def signal_to_dict(m: MomentumSignal) -> dict:
    return {
        "ticker": m.ticker, "signal": m.signal, "score": m.score,
        "confidence": m.confidence, "entry": m.entry, "stop_loss": m.stop_loss,
        "target": m.target, "risk_reward": m.risk_reward,
        "close_range_pct": m.close_range_pct, "rel_volume": m.rel_volume,
        "vwap_dist_pct": m.vwap_dist_pct, "gap_pct": m.gap_pct,
        "atr_pct": m.atr_pct, "hold": m.hold, "reasons": m.reasons,
    }
