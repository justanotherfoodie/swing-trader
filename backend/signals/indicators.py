"""Technical indicator calculations — pure pandas/numpy, no external TA library needed."""

import pandas as pd
import numpy as np


# ── Core indicator functions ──────────────────────────────────────────────────

def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=length - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=length - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bbands(series: pd.Series, length=20, std=2):
    middle = series.rolling(length).mean()
    sigma = series.rolling(length).std()
    upper = middle + std * sigma
    lower = middle - std * sigma
    width = (upper - lower) / middle.replace(0, np.nan)
    return upper, middle, lower, width


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length=14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=length - 1, adjust=False).mean()


# ── Main enrichment function ──────────────────────────────────────────────────

def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicators needed by the 5 strategies."""
    df = df.copy()
    c = df["close"]
    h = df["high"]
    lo = df["low"]

    # EMAs
    df["ema9"]   = _ema(c, 9)
    df["ema21"]  = _ema(c, 21)
    df["ema50"]  = _ema(c, 50)
    df["ema200"] = _ema(c, 200)

    # RSI
    df["rsi"] = _rsi(c, 14)

    # MACD
    ml, sl, hist = _macd(c)
    df["macd"]        = ml
    df["macd_signal"] = sl
    df["macd_hist"]   = hist

    # Bollinger Bands
    bb_u, bb_m, bb_l, bb_w = _bbands(c, 20, 2)
    df["bb_upper"]  = bb_u
    df["bb_middle"] = bb_m
    df["bb_lower"]  = bb_l
    df["bb_width"]  = bb_w

    # ATR
    df["atr"] = _atr(h, lo, c, 14)

    # Volume ratio vs 20-day average
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    # Support / Resistance: rolling 20-day low/high
    df["support"]    = lo.rolling(20).min()
    df["resistance"] = h.rolling(20).max()

    return df


def find_support_resistance(df: pd.DataFrame, lookback: int = 60) -> tuple[float, float]:
    """Return nearest support and resistance to current price."""
    recent = df.tail(lookback)
    lows  = float(recent["low"].nsmallest(5).mean())
    highs = float(recent["high"].nlargest(5).mean())
    return round(lows, 2), round(highs, 2)


def is_reversal_candle(df: pd.DataFrame) -> bool:
    """Detect hammer or bullish engulfing on last candle."""
    if len(df) < 2:
        return False
    c = df.iloc[-1]
    p = df.iloc[-2]
    body = abs(float(c["close"]) - float(c["open"]))
    lower_wick = min(float(c["open"]), float(c["close"])) - float(c["low"])
    # Hammer: lower wick >= 2x body
    if body > 0 and lower_wick >= 2 * body:
        return True
    # Bullish engulfing
    if (float(c["close"]) > float(p["open"])
            and float(c["open"]) < float(p["close"])
            and float(p["close"]) < float(p["open"])):
        return True
    return False
