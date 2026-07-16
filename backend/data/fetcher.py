"""Market data fetcher using yfinance with caching."""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib, time

_cache: dict = {}
CACHE_TTL = 900  # 15 min


def _cache_key(ticker: str, period: str) -> str:
    return f"{ticker}:{period}"


def _drop_partial_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Drop today's still-forming daily candle during US market hours.

    yfinance returns an in-progress bar for the current session while the market is
    open. Crossover/RSI/MACD math on a partial bar produces false signals that vanish
    by the close, so we only act on completed daily candles.
    """
    if df.empty:
        return df
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        last_bar_date = df.index[-1].date()
        market_open = now_et.weekday() < 5 and (9 * 60 + 30) <= (now_et.hour * 60 + now_et.minute) < (16 * 60)
        if market_open and last_bar_date == now_et.date():
            return df.iloc[:-1]
    except Exception:
        pass
    return df


def get_ohlcv(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    key = _cache_key(ticker, period)
    now = time.time()
    if key in _cache and (now - _cache[key]["ts"]) < CACHE_TTL:
        return _cache[key]["df"]

    try:
        df = yf.download(ticker, period=period, interval=interval,
                         auto_adjust=True, progress=False, threads=False)
        if df.empty:
            return pd.DataFrame()
        df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        df = df.dropna()
        if interval == "1d":
            df = _drop_partial_bar(df)
        _cache[key] = {"df": df, "ts": now}
        return df
    except Exception as e:
        print(f"[fetcher] Error fetching {ticker}: {e}")
        return pd.DataFrame()


def get_batch_ohlcv(tickers: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    results = {}
    for ticker in tickers:
        df = get_ohlcv(ticker, period)
        if not df.empty:
            results[ticker] = df
    return results


def get_current_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        return float(info.last_price)
    except Exception:
        df = get_ohlcv(ticker, period="5d")
        if not df.empty:
            return float(df["close"].iloc[-1])
        return None


def get_ticker_info(ticker: str) -> dict:
    try:
        t = yf.Ticker(ticker)
        info = t.info
        return {
            "name": info.get("longName", ticker),
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
            "market_cap": info.get("marketCap", 0),
            "pe_ratio": info.get("trailingPE"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "avg_volume": info.get("averageVolume"),
        }
    except Exception:
        return {"name": ticker, "sector": "Unknown"}
