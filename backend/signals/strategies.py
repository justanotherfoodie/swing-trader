"""
5 Swing Trading Strategies - each returns a StrategyResult.

Strategies implemented (research-backed):
  1. RSI Mean Reversion     - oversold bounce at support (trend-filtered)
  2. MACD + RSI Confluence  - momentum with room to run (73% win rate per backtests)
  3. EMA 9/21 Crossover     - classic trend momentum signal
  4. Bollinger Band Squeeze - volatility contraction -> breakout
  5. EMA 50 + BB Trend      - trend continuation in strong uptrend

Each result carries `is_trigger`: True when the strategy detects a FRESH, actionable
event (a crossover, breakout, or extreme bounce) rather than a passive "trend is up"
state read. The scorer requires at least one trigger before issuing a BUY/SELL so the
system stops flagging already-extended stocks that merely happen to be trending.
"""

from dataclasses import dataclass
import pandas as pd
from .indicators import is_reversal_candle


@dataclass
class StrategyResult:
    name: str
    signal: int          # +1 BUY, -1 SELL, 0 NEUTRAL
    score: float         # -2.0 to +2.0 strength
    reason: str
    is_trigger: bool = False   # True = fresh actionable event, False = state/context


def rsi_mean_reversion(df: pd.DataFrame) -> StrategyResult:
    """RSI < 30 at support + reversal candle -> BUY. Trend-filtered to avoid falling knives."""
    if len(df) < 20 or "rsi" not in df.columns:
        return StrategyResult("RSI Mean Reversion", 0, 0, "Insufficient data")

    rsi   = float(df["rsi"].iloc[-1])
    close = float(df["close"].iloc[-1])
    sup   = float(df["support"].iloc[-1])
    res   = float(df["resistance"].iloc[-1])
    e50   = float(df["ema50"].iloc[-1]) if "ema50" in df.columns else close
    e200  = float(df["ema200"].iloc[-1]) if "ema200" in df.columns else close
    rev   = is_reversal_candle(df)

    # Falling-knife filter: don't buy oversold when the long-term trend is firmly down.
    strong_downtrend = e50 < e200 * 0.97

    if rsi < 30:
        near_support = close <= sup * 1.03
        if strong_downtrend and not rev:
            # Oversold but no reversal confirmation in a downtrend = knife. Skip.
            return StrategyResult("RSI Mean Reversion", 0, 0,
                                  f"RSI={rsi:.1f} oversold but downtrend, no reversal - avoid knife")
        score = 2.0 if (near_support and rev) else (1.5 if near_support else 1.2)
        if strong_downtrend:
            score *= 0.6  # counter-trend bounce, smaller size
        reason = f"RSI={rsi:.1f} strongly oversold" + (", at support" if near_support else "") + (", reversal candle" if rev else "") + (" (counter-trend)" if strong_downtrend else "")
        return StrategyResult("RSI Mean Reversion", 1, round(score, 2), reason, is_trigger=True)

    if 30 <= rsi < 35:
        if strong_downtrend and not rev:
            return StrategyResult("RSI Mean Reversion", 0, 0,
                                  f"RSI={rsi:.1f} oversold zone but downtrend - wait for reversal")
        score = 1.0 + (0.5 if rev else 0)
        reason = f"RSI={rsi:.1f} oversold zone" + (", reversal candle" if rev else "")
        return StrategyResult("RSI Mean Reversion", 1, score, reason, is_trigger=rev)

    if rsi > 70:
        near_res = close >= res * 0.97
        score = -2.0 if near_res else -1.2
        reason = f"RSI={rsi:.1f} strongly overbought" + (", at resistance" if near_res else "")
        return StrategyResult("RSI Mean Reversion", -1, score, reason, is_trigger=True)

    if 65 < rsi <= 70:
        return StrategyResult("RSI Mean Reversion", -1, -1.0,
                              f"RSI={rsi:.1f} entering overbought", is_trigger=False)

    return StrategyResult("RSI Mean Reversion", 0, 0, f"RSI={rsi:.1f} neutral zone")


def macd_rsi_confluence(df: pd.DataFrame) -> StrategyResult:
    """MACD bullish crossover + RSI 40-60 (not overbought, has room) -> BUY."""
    required = ["macd", "macd_signal", "macd_hist", "rsi"]
    if len(df) < 30 or not all(c in df.columns for c in required):
        return StrategyResult("MACD+RSI Confluence", 0, 0, "Insufficient data")

    macd      = float(df["macd"].iloc[-1])
    macd_prev = float(df["macd"].iloc[-2])
    sig       = float(df["macd_signal"].iloc[-1])
    sig_prev  = float(df["macd_signal"].iloc[-2])
    hist      = float(df["macd_hist"].iloc[-1])
    hist_prev = float(df["macd_hist"].iloc[-2])
    rsi       = float(df["rsi"].iloc[-1])

    bull_cross = macd_prev < sig_prev and macd > sig
    bear_cross = macd_prev > sig_prev and macd < sig

    if bull_cross and 35 <= rsi <= 65:
        score = 2.0 if rsi < 55 else 1.5
        return StrategyResult("MACD+RSI Confluence", 1, score,
                              f"MACD bullish cross, RSI={rsi:.1f} has room to run", is_trigger=True)

    if bull_cross and rsi < 35:
        return StrategyResult("MACD+RSI Confluence", 1, 1.5,
                              f"MACD bullish cross, RSI={rsi:.1f} oversold - strong", is_trigger=True)

    if bull_cross and rsi > 65:
        return StrategyResult("MACD+RSI Confluence", 1, 0.5,
                              f"MACD bullish cross but RSI={rsi:.1f} already extended", is_trigger=True)

    if bear_cross and rsi > 55:
        score = -2.0 if rsi > 65 else -1.5
        return StrategyResult("MACD+RSI Confluence", -1, score,
                              f"MACD bearish cross, RSI={rsi:.1f} overbought", is_trigger=True)

    if bear_cross:
        return StrategyResult("MACD+RSI Confluence", -1, -1.0,
                              f"MACD bearish cross, RSI={rsi:.1f}", is_trigger=True)

    # Histogram momentum = context, NOT a fresh trigger.
    #
    # Tested neutralising this branch on the theory that its low-conviction nudge was
    # diluting the signal pool. It made results WORSE (-$5.16/trade vs -$3.27 baseline),
    # so the theory was wrong: the damage in this strategy comes from the crossover
    # triggers above, not from this. Left at its original weight - see DISABLED_STRATEGIES
    # in signals/scorer.py for how the evidence was actually acted on.
    if hist > 0 and hist > hist_prev and macd > sig and 40 <= rsi <= 60:
        return StrategyResult("MACD+RSI Confluence", 1, 0.6,
                              f"MACD above signal, momentum building, RSI={rsi:.1f}", is_trigger=False)

    return StrategyResult("MACD+RSI Confluence", 0, 0,
                          f"No crossover. MACD={'above' if macd>sig else 'below'} signal")


def ema_crossover(df: pd.DataFrame) -> StrategyResult:
    """9 EMA crosses above 21 EMA -> BUY momentum; below -> SELL."""
    if len(df) < 25 or "ema9" not in df.columns:
        return StrategyResult("EMA 9/21 Crossover", 0, 0, "Insufficient data")

    e9      = float(df["ema9"].iloc[-1])
    e21     = float(df["ema21"].iloc[-1])
    e9_p    = float(df["ema9"].iloc[-2])
    e21_p   = float(df["ema21"].iloc[-2])
    e50     = float(df["ema50"].iloc[-1])
    close   = float(df["close"].iloc[-1])

    fresh_bull = e9_p < e21_p and e9 > e21
    fresh_bear = e9_p > e21_p and e9 < e21
    above_50   = close > e50

    if fresh_bull:
        score = 2.0 if above_50 else 1.5
        return StrategyResult("EMA 9/21 Crossover", 1, score,
                              "9 EMA crossed above 21 EMA" + (" - above 50 EMA, strong uptrend" if above_50 else ""),
                              is_trigger=True)

    if fresh_bear:
        score = -2.0 if not above_50 else -1.5
        return StrategyResult("EMA 9/21 Crossover", -1, score,
                              "9 EMA crossed below 21 EMA" + (" - below 50 EMA, downtrend" if not above_50 else ""),
                              is_trigger=True)

    # Continuation = context only (NOT a trigger)
    if e9 > e21 and above_50:
        return StrategyResult("EMA 9/21 Crossover", 1, 0.5,
                              "9 EMA > 21 EMA, price above 50 EMA - uptrend intact", is_trigger=False)

    if e9 < e21 and not above_50:
        return StrategyResult("EMA 9/21 Crossover", -1, -0.5,
                              "9 EMA < 21 EMA, price below 50 EMA - downtrend intact", is_trigger=False)

    return StrategyResult("EMA 9/21 Crossover", 0, 0,
                          f"9 EMA {'>' if e9>e21 else '<'} 21 EMA, no fresh cross")


def bb_squeeze_breakout(df: pd.DataFrame) -> StrategyResult:
    """BB bandwidth at recent low (squeeze) then price breaks above upper band w/ volume."""
    required = ["bb_upper", "bb_lower", "bb_width", "vol_ratio"]
    if len(df) < 25 or not all(c in df.columns for c in required):
        return StrategyResult("BB Squeeze Breakout", 0, 0, "Insufficient data")

    bw      = df["bb_width"].dropna()
    cur_bw  = float(bw.iloc[-1])
    min_bw  = float(bw.tail(60).min())
    close   = float(df["close"].iloc[-1])
    close_p = float(df["close"].iloc[-2])
    upper   = float(df["bb_upper"].iloc[-1])
    lower   = float(df["bb_lower"].iloc[-1])
    vol_r   = float(df["vol_ratio"].iloc[-1])

    squeeze = cur_bw <= min_bw * 1.25

    if squeeze and close > upper and close_p <= upper and vol_r > 1.4:
        return StrategyResult("BB Squeeze Breakout", 1, 2.0,
                              f"Squeeze breakout above upper BB with {vol_r:.1f}x volume", is_trigger=True)

    if squeeze and close < lower and close_p >= lower and vol_r > 1.4:
        return StrategyResult("BB Squeeze Breakout", -1, -2.0,
                              f"Squeeze breakdown below lower BB with {vol_r:.1f}x volume", is_trigger=True)

    if squeeze and close > upper:
        return StrategyResult("BB Squeeze Breakout", 1, 1.0,
                              "Post-squeeze breakout above upper BB (weak volume)", is_trigger=True)

    if squeeze:
        return StrategyResult("BB Squeeze Breakout", 0, 0.3,
                              f"BB squeeze active (bw={cur_bw:.3f}) - awaiting breakout", is_trigger=False)

    return StrategyResult("BB Squeeze Breakout", 0, 0,
                          f"No squeeze. BW={cur_bw:.3f}")


def ema50_bb_trend(df: pd.DataFrame) -> StrategyResult:
    """Price bounces off 50 EMA while above middle BB -> trend continuation BUY."""
    required = ["ema50", "bb_middle", "bb_upper", "bb_lower", "ema200"]
    if len(df) < 55 or not all(c in df.columns for c in required):
        return StrategyResult("EMA50+BB Trend", 0, 0, "Insufficient data")

    close    = float(df["close"].iloc[-1])
    low      = float(df["low"].iloc[-1])
    low_p    = float(df["low"].iloc[-2])
    e50      = float(df["ema50"].iloc[-1])
    e200     = float(df["ema200"].iloc[-1])
    bb_mid   = float(df["bb_middle"].iloc[-1])
    bb_upper = float(df["bb_upper"].iloc[-1])
    rsi      = float(df["rsi"].iloc[-1]) if "rsi" in df.columns else 50

    long_uptrend = e50 > e200
    above_mid_bb = close > bb_mid

    # Fresh pullback bounce: today's low tagged the 50 EMA and closed back above it.
    fresh_bounce = low <= e50 * 1.01 and close > e50 and low_p > e50 * 1.01
    if fresh_bounce and above_mid_bb and long_uptrend:
        score = 2.0 if rsi < 55 else 1.5
        return StrategyResult("EMA50+BB Trend", 1, score,
                              "Fresh bounce off 50 EMA in uptrend, above BB midline", is_trigger=True)

    # Sitting on the 50 EMA in uptrend (actionable but not a fresh bounce)
    if low <= e50 * 1.01 and close > e50 and above_mid_bb and long_uptrend:
        return StrategyResult("EMA50+BB Trend", 1, 1.0,
                              "Holding 50 EMA support in uptrend", is_trigger=True)

    # Price riding upper band = strong trend context (not a fresh entry)
    if close > bb_upper * 0.98 and above_mid_bb and long_uptrend:
        return StrategyResult("EMA50+BB Trend", 1, 0.5,
                              "Price riding upper BB in strong uptrend", is_trigger=False)

    if not long_uptrend and not above_mid_bb:
        return StrategyResult("EMA50+BB Trend", -1, -1.0,
                              "50 EMA below 200 EMA, price below BB midline - downtrend", is_trigger=False)

    return StrategyResult("EMA50+BB Trend", 0, 0,
                          f"No clear EMA50/BB setup. Uptrend={long_uptrend}")


ALL_STRATEGIES = [
    rsi_mean_reversion,
    macd_rsi_confluence,
    ema_crossover,
    bb_squeeze_breakout,
    ema50_bb_trend,
]
