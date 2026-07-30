"""Market data fetcher using yfinance with caching.

Failure semantics
-----------------
Every public function keeps its historical signature and return type, so a
failed fetch still looks like an empty ``DataFrame`` / ``None`` / a stub dict to
existing callers. To let callers tell "the feed is down" apart from "this ticker
genuinely has nothing", each failure is recorded in :mod:`data.resilience`:

    from data.resilience import get_data_health, get_last_error, is_degraded

Source names used here: ``yfinance.ohlcv``, ``yfinance.price``,
``yfinance.info``.
"""

import logging
import time
from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
import yfinance as yf

from .resilience import (
    MISSING,
    DataUnavailable,
    call_with_retry,
    record_failure,
    record_missing,
    record_success,
    run_with_timeout,
)

logger = logging.getLogger(__name__)

_cache: dict = {}
CACHE_TTL = 900  # 15 min

#: Confirmed-missing (delisted / unknown) symbols are remembered for a shorter
#: window so a ~900-ticker scan does not re-request known-dead names every pass,
#: while still allowing recovery within the hour. NOTE: only *confirmed missing*
#: results land here — a failed fetch never writes to any cache.
MISSING_TTL = 3600
_missing: dict = {}

#: Wall-clock timeout for a single yfinance download request.
DOWNLOAD_TIMEOUT = 12
#: Timeout for the metadata endpoints that expose no timeout parameter.
INFO_TIMEOUT = 8
#: Upper bound on wall-clock time spent retrying a single ticker. With ~900
#: tickers per scan this is what keeps a degraded feed from ballooning runtime
#: (the circuit breaker in resilience.py suspends retries entirely once the
#: outage is broad).
RETRY_DEADLINE = 3.0

OHLCV_SOURCE = "yfinance.ohlcv"
PRICE_SOURCE = "yfinance.price"
INFO_SOURCE = "yfinance.info"


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
    except (ImportError, AttributeError, IndexError, ValueError) as e:
        # Bad/missing tz database or a non-datetime index: keep the frame as-is
        # rather than dropping a bar we cannot reason about.
        logger.debug("partial-bar check skipped: %s: %s", type(e).__name__, e)
    return df


def _is_missing(key: str, now: float) -> bool:
    ts = _missing.get(key)
    if ts is None:
        return False
    if (now - ts) >= MISSING_TTL:
        _missing.pop(key, None)
        return False
    return True


def _download(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
        threads=False,
        timeout=DOWNLOAD_TIMEOUT,
    )
    if df is None:
        # yfinance returns None on some failure paths; treat as a real failure,
        # not as "no data", so it is retried and surfaced in the health report.
        raise DataUnavailable(f"yfinance returned None for {ticker}")
    return df


def get_ohlcv(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Daily OHLCV frame (lower-cased columns). Empty frame on missing OR failure.

    Consult ``resilience.get_data_health()`` to distinguish the two.
    """
    key = _cache_key(ticker, period)
    now = time.time()
    if key in _cache and (now - _cache[key]["ts"]) < CACHE_TTL:
        return _cache[key]["df"]
    if _is_missing(key, now):
        return pd.DataFrame()

    try:
        df = call_with_retry(
            lambda: _download(ticker, period, interval),
            source=OHLCV_SOURCE,
            subject=ticker,
            deadline=RETRY_DEADLINE,
        )
    except DataUnavailable as e:
        if e.kind == MISSING:
            record_missing(OHLCV_SOURCE, ticker)
            _missing[key] = now
        else:
            record_failure(OHLCV_SOURCE, e.cause or e, subject=ticker, kind=e.kind)
            logger.error("fetch failed for %s (%s): %s", ticker, e.kind, e)
        # Never cache a failed fetch: the next call must retry.
        return pd.DataFrame()

    if df.empty:
        # Upstream answered successfully with zero rows -> genuinely no data.
        record_missing(OHLCV_SOURCE, ticker)
        _missing[key] = now
        return pd.DataFrame()

    try:
        df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        df = df.dropna()
        if interval == "1d":
            df = _drop_partial_bar(df)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        # Malformed payload — a data-quality failure, not "no signal".
        record_failure(
            OHLCV_SOURCE, e, subject=ticker, detail=f"malformed frame: {e}"
        )
        logger.error("malformed OHLCV payload for %s: %s: %s", ticker, type(e).__name__, e)
        return pd.DataFrame()

    if df.empty:
        # Everything dropped out (all-NaN, or a lone partial bar). Real result,
        # but do not cache it as authoritative history.
        record_success(OHLCV_SOURCE)
        return df

    record_success(OHLCV_SOURCE)
    _cache[key] = {"df": df, "ts": now}
    _missing.pop(key, None)
    return df


def get_batch_ohlcv(tickers: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    results = {}
    for ticker in tickers:
        df = get_ohlcv(ticker, period)
        if not df.empty:
            results[ticker] = df
    return results


def get_current_price(ticker: str) -> float | None:
    def _fast_price() -> float:
        return float(yf.Ticker(ticker).fast_info.last_price)

    try:
        price = call_with_retry(
            lambda: run_with_timeout(
                _fast_price, INFO_TIMEOUT, what=f"fast_info({ticker})"
            ),
            source=PRICE_SOURCE,
            subject=ticker,
            deadline=RETRY_DEADLINE,
        )
        if price is None or price != price or price <= 0:  # NaN / nonsense guard
            raise DataUnavailable(f"fast_info returned {price!r} for {ticker}")
        record_success(PRICE_SOURCE)
        return price
    except (DataUnavailable, TypeError, ValueError, AttributeError) as e:
        kind = getattr(e, "kind", None)
        if kind == MISSING:
            record_missing(PRICE_SOURCE, ticker)
        else:
            record_failure(PRICE_SOURCE, getattr(e, "cause", None) or e,
                           subject=ticker, kind=kind)
        logger.info("fast_info unavailable for %s (%s) — falling back to OHLCV close",
                    ticker, e)

    # Preserved fallback: last close from the (cached) daily history.
    df = get_ohlcv(ticker, period="5d")
    if not df.empty:
        return float(df["close"].iloc[-1])
    return None


def get_ticker_info(ticker: str) -> dict:
    def _info() -> dict:
        return yf.Ticker(ticker).info or {}

    try:
        info = call_with_retry(
            lambda: run_with_timeout(_info, INFO_TIMEOUT, what=f"info({ticker})"),
            source=INFO_SOURCE,
            subject=ticker,
            deadline=RETRY_DEADLINE,
        )
    except DataUnavailable as e:
        if e.kind == MISSING:
            record_missing(INFO_SOURCE, ticker)
            logger.debug("no profile info for %s", ticker)
        else:
            record_failure(INFO_SOURCE, e.cause or e, subject=ticker, kind=e.kind)
            logger.warning("ticker info unavailable for %s (%s): %s", ticker, e.kind, e)
        return {"name": ticker, "sector": "Unknown"}

    record_success(INFO_SOURCE)
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
