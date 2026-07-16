"""News fetcher — NewsAPI for ticker headlines + macro events."""

import os, requests, json
from datetime import datetime, timedelta
from dotenv import load_dotenv

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


def _cached(key: str, fetch_fn):
    import time
    now = time.time()
    if key in _news_cache and (now - _news_cache[key]["ts"]) < CACHE_TTL:
        return _news_cache[key]["data"]
    data = fetch_fn()
    _news_cache[key] = {"data": data, "ts": now}
    return data


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
            r = requests.get(url, params=params, timeout=10)
            articles = r.json().get("articles", [])
            return [
                {
                    "title": a["title"],
                    "source": a["source"]["name"],
                    "published": a["publishedAt"],
                    "url": a["url"],
                    "description": a.get("description", ""),
                }
                for a in articles if a.get("title")
            ]
        except Exception as e:
            print(f"[news] Error fetching {ticker}: {e}")
            return []

    return _cached(f"ticker:{ticker}", fetch)


def get_macro_news(days: int = 2) -> list[dict]:
    def fetch():
        if not NEWS_API_KEY:
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
            r = requests.get(url, params=params, timeout=10)
            articles = r.json().get("articles", [])
            return [
                {
                    "title": a["title"],
                    "source": a["source"]["name"],
                    "published": a["publishedAt"],
                    "url": a["url"],
                }
                for a in articles if a.get("title")
            ]
        except Exception as e:
            print(f"[news] Macro fetch error: {e}")
            return []

    return _cached("macro", fetch)


def _fallback_news(ticker: str) -> list[dict]:
    """Scrape Yahoo Finance headlines as fallback when no API key."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        news = t.news or []
        return [
            {
                "title": n.get("title", ""),
                "source": n.get("publisher", "Yahoo Finance"),
                "published": datetime.fromtimestamp(n.get("providerPublishTime", 0)).isoformat(),
                "url": n.get("link", ""),
                "description": "",
            }
            for n in news[:10]
        ]
    except Exception:
        return []
