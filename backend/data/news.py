"""News fetcher — NewsAPI for ticker headlines + macro events.

All public functions keep their historical return type (``list[dict]``), so a
failed fetch still yields ``[]``. Whether that ``[]`` means "no headlines" or
"NewsAPI is down" is recorded in :mod:`data.resilience` under the source names
``newsapi.ticker``, ``newsapi.macro`` and ``yfinance.news``.
"""

import logging
import os
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

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

load_dotenv()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

MACRO_KEYWORDS = [
    "Federal Reserve", "FOMC", "interest rate", "inflation", "CPI", "PPI",
    "unemployment", "GDP", "recession", "yield curve", "Treasury", "tariff",
    "trade war", "earnings season", "S&P 500", "Nasdaq", "market crash",
    "bull market", "bear market", "quantitative tightening"
]

_news_cache: dict = {}
CACHE_TTL = 1800  # 30 min

#: Every NewsAPI request gets an explicit connect/read timeout.
HTTP_TIMEOUT = (5, 10)
#: yfinance news has no timeout parameter — bound it with a watchdog thread.
YF_NEWS_TIMEOUT = 8
#: Cap on wall-clock retry time per news call.
RETRY_DEADLINE = 4.0

TICKER_SOURCE = "newsapi.ticker"
MACRO_SOURCE = "newsapi.macro"
YF_NEWS_SOURCE = "yfinance.news"


#: An empty list may be the result of a failure, so it gets a much shorter
#: lifetime than real headlines — enough to avoid hammering the API inside one
#: scan, short enough that a recovered feed is picked up quickly.
EMPTY_CACHE_TTL = 300


def _cached(key: str, fetch_fn):
    now = time.time()
    entry = _news_cache.get(key)
    if entry is not None:
        ttl = CACHE_TTL if entry["data"] else EMPTY_CACHE_TTL
        if (now - entry["ts"]) < ttl:
            return entry["data"]
    data = fetch_fn()
    _news_cache[key] = {"data": data, "ts": now}
    return data


def _newsapi_get(url: str, params: dict, source: str, subject: str | None) -> list[dict]:
    """Single NewsAPI request with explicit timeout and status handling."""
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    if r.status_code == 429:
        raise requests.exceptions.HTTPError("429 rate limited by NewsAPI", response=r)
    if r.status_code >= 500:
        raise requests.exceptions.HTTPError(
            f"{r.status_code} NewsAPI server error", response=r
        )
    try:
        payload = r.json()
    except ValueError as e:
        raise requests.exceptions.HTTPError(
            f"non-JSON NewsAPI response ({r.status_code})", response=r
        ) from e
    if payload.get("status") == "error":
        # e.g. apiKeyInvalid / rateLimited — a real failure, not "no news".
        code = payload.get("code", "unknown")
        exc = requests.exceptions.HTTPError(
            f"NewsAPI error {code}: {payload.get('message', '')}", response=r
        )
        if code in ("rateLimited", "maximumResultsReached"):
            raise exc
        raise DataUnavailable(str(exc), cause=exc)
    if r.status_code >= 400:
        raise DataUnavailable(f"NewsAPI HTTP {r.status_code}")
    return payload.get("articles") or []


def get_ticker_news(ticker: str, days: int = 3) -> list[dict]:
    def fetch():
        if not NEWS_API_KEY:
            return _fallback_news(ticker)
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": ticker,
            "from": from_date,
            "sortBy": "relevancy",
            "language": "en",
            "pageSize": 10,
            "apiKey": NEWS_API_KEY,
        }
        try:
            articles = call_with_retry(
                lambda: _newsapi_get(url, params, TICKER_SOURCE, ticker),
                source=TICKER_SOURCE,
                subject=ticker,
                deadline=RETRY_DEADLINE,
            )
        except DataUnavailable as e:
            record_failure(TICKER_SOURCE, e.cause or e, subject=ticker, kind=e.kind)
            logger.error("ticker news unavailable for %s (%s): %s", ticker, e.kind, e)
            # Preserved behaviour: fall back to the free yfinance feed rather
            # than returning nothing when the paid API is unreachable.
            return _fallback_news(ticker)

        record_success(TICKER_SOURCE)
        if not articles:
            record_missing(TICKER_SOURCE, ticker)
        return [
            {
                "title": a["title"],
                "source": (a.get("source") or {}).get("name", "Unknown"),
                "published": a.get("publishedAt", ""),
                "url": a.get("url", ""),
                "description": a.get("description", ""),
            }
            for a in articles if a.get("title")
        ]

    return _cached(f"ticker:{ticker}", fetch)


def get_macro_news(days: int = 2) -> list[dict]:
    def fetch():
        if not NEWS_API_KEY:
            logger.info("NEWS_API_KEY not set — macro news disabled")
            return []
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        query = " OR ".join(f'"{kw}"' for kw in MACRO_KEYWORDS[:6])
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "from": from_date,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": 15,
            "apiKey": NEWS_API_KEY,
        }
        try:
            articles = call_with_retry(
                lambda: _newsapi_get(url, params, MACRO_SOURCE, "macro"),
                source=MACRO_SOURCE,
                subject="macro",
                deadline=RETRY_DEADLINE,
            )
        except DataUnavailable as e:
            record_failure(MACRO_SOURCE, e.cause or e, subject="macro", kind=e.kind)
            logger.error("macro news unavailable (%s): %s", e.kind, e)
            return []

        record_success(MACRO_SOURCE)
        if not articles:
            record_missing(MACRO_SOURCE, "macro")
        return [
            {
                "title": a["title"],
                "source": (a.get("source") or {}).get("name", "Unknown"),
                "published": a.get("publishedAt", ""),
                "url": a.get("url", ""),
            }
            for a in articles if a.get("title")
        ]

    return _cached("macro", fetch)


def _normalize_yf_item(n: dict) -> dict | None:
    """Normalize a yfinance news entry.

    yfinance <1.0 returned flat dicts (``title``/``publisher``/``link``/
    ``providerPublishTime``); >=1.0 nests everything under ``content``. Handle
    both so the fallback keeps working across versions.
    """
    if not isinstance(n, dict):
        return None
    content = n.get("content") if isinstance(n.get("content"), dict) else None
    if content:
        title = content.get("title") or ""
        publisher = (content.get("provider") or {}).get("displayName", "Yahoo Finance")
        published = content.get("pubDate") or ""
        url = (
            (content.get("canonicalUrl") or {}).get("url")
            or (content.get("clickThroughUrl") or {}).get("url")
            or ""
        )
        description = content.get("summary") or ""
    else:
        title = n.get("title") or ""
        publisher = n.get("publisher") or "Yahoo Finance"
        ts = n.get("providerPublishTime") or 0
        try:
            published = datetime.fromtimestamp(ts).isoformat() if ts else ""
        except (OverflowError, OSError, ValueError):
            published = ""
        url = n.get("link") or ""
        description = ""
    if not title:
        return None
    return {
        "title": title,
        "source": publisher,
        "published": published,
        "url": url,
        "description": description,
    }


def _fallback_news(ticker: str) -> list[dict]:
    """Yahoo Finance headlines as fallback when NewsAPI is absent or failing."""
    def _fetch() -> list:
        import yfinance as yf
        return yf.Ticker(ticker).news or []

    try:
        news = call_with_retry(
            lambda: run_with_timeout(_fetch, YF_NEWS_TIMEOUT, what=f"news({ticker})"),
            source=YF_NEWS_SOURCE,
            subject=ticker,
            deadline=RETRY_DEADLINE,
        )
    except DataUnavailable as e:
        if e.kind == MISSING:
            record_missing(YF_NEWS_SOURCE, ticker)
            logger.debug("no yfinance news for %s", ticker)
        else:
            record_failure(YF_NEWS_SOURCE, e.cause or e, subject=ticker, kind=e.kind)
            logger.warning("yfinance news unavailable for %s (%s): %s", ticker, e.kind, e)
        return []

    record_success(YF_NEWS_SOURCE)
    if not news:
        record_missing(YF_NEWS_SOURCE, ticker)
    items = [_normalize_yf_item(n) for n in news[:10]]
    return [i for i in items if i]
