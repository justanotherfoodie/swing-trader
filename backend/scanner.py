"""
Market scanner — runs across the S&P 500 universe and returns top signals.
Designed to run on a schedule during market hours.
"""

import time
import concurrent.futures
from dataclasses import asdict

from data.fetcher import get_ohlcv
from data.news import get_ticker_news, get_macro_news
from data.universe import get_sp500_tickers
from signals.scorer import score_ticker, TradeSignal
from signals.options import build_options_play, play_to_dict
from ai.analyzer import score_macro_sentiment, score_ticker_sentiment, generate_trade_rationale

# Shared state — last scan results
_last_scan: dict = {
    "signals": [],
    "macro": {"score": 0.0, "summary": "", "themes": []},
    "scanned_at": None,
    "total_scanned": 0,
}


def _signal_to_dict(s: TradeSignal, news_summary: str = "", rationale: str = "",
                    options_play: dict | None = None) -> dict:
    return {
        "ticker": s.ticker,
        "signal": s.signal,
        "confidence": s.confidence,
        "entry": s.entry,
        "stop_loss": s.stop_loss,
        "take_profit_1": s.take_profit_1,
        "take_profit_2": s.take_profit_2,
        "atr": s.atr,
        "atr_pct": s.atr_pct,
        "rsi": s.rsi,
        "total_score": s.total_score,
        "macro_score": s.macro_score,
        "holding_days": s.holding_days,
        "support": s.support,
        "resistance": s.resistance,
        "risk_per_share": s.risk_per_share,
        "risk_reward": s.risk_reward,
        "shares": s.shares,
        "position_value": s.position_value,
        "quality": s.quality,
        "target_note": s.target_note,
        "triggers": s.triggers,
        "reasons": s.reasons,
        "strategy_breakdown": [
            {"name": r.name, "signal": r.signal, "score": r.score,
             "reason": r.reason, "is_trigger": r.is_trigger}
            for r in s.strategy_results
        ],
        "news_summary": news_summary,
        "rationale": rationale,
        "options_play": options_play,
    }


_QUALITY_RANK = {"high": 0, "medium": 1, "low": 2}


def run_scan(max_tickers: int = 1200, top_n: int = 25) -> dict:
    """Full market scan. Returns top N BUY and SELL signals."""
    print("[scanner] Starting market scan...")
    t0 = time.time()

    # Step 1: Macro sentiment
    macro_headlines = get_macro_news()
    macro_score, macro_summary = score_macro_sentiment(macro_headlines)
    print(f"[scanner] Macro score: {macro_score:.2f} - {macro_summary[:80]}")

    # Step 2: Score all tickers
    tickers = get_sp500_tickers()[:max_tickers]
    raw_signals: list[TradeSignal] = []

    def process(ticker: str):
        df = get_ohlcv(ticker, period="6mo", interval="1d")
        if df is None or df.empty:
            return None
        return score_ticker(ticker, df, macro_score)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(process, t): t for t in tickers}
        for fut in concurrent.futures.as_completed(futures):
            result = fut.result()
            if result is not None:
                raw_signals.append(result)

    print(f"[scanner] Scored {len(raw_signals)} tickers in {time.time()-t0:.1f}s")

    # Step 3: Rank by quality grade, then confidence, then conviction.
    # A high-quality BUY with good R:R outranks a higher-score trade with a bad target.
    buys  = sorted([s for s in raw_signals if s.signal == "BUY"],
                   key=lambda x: (_QUALITY_RANK[x.quality], -x.confidence, -x.total_score))[:top_n]
    sells = sorted([s for s in raw_signals if s.signal == "SELL"],
                   key=lambda x: (_QUALITY_RANK[x.quality], -x.confidence, x.total_score))[:top_n]

    top_signals = buys + sells

    # Step 4: Enrich top signals with news + AI rationale
    enriched = []
    for sig in top_signals:
        try:
            headlines = get_ticker_news(sig.ticker, days=3)
            news_score, news_summary = score_ticker_sentiment(sig.ticker, headlines)
            # Adjust confidence with news score (bounded)
            rationale = generate_trade_rationale(
                ticker=sig.ticker,
                signal=sig.signal,
                confidence=sig.confidence,
                entry=sig.entry,
                stop_loss=sig.stop_loss,
                take_profit_1=sig.take_profit_1,
                strategy_reasons=sig.reasons,
                macro_summary=macro_summary,
                news_summary=news_summary,
            )
            # Defined-risk options spread aligned to the signal direction + target.
            opt = None
            if sig.signal in ("BUY", "SELL"):
                opt = play_to_dict(build_options_play(
                    sig.ticker, sig.signal, sig.entry, sig.take_profit_1, sig.stop_loss))
            enriched.append(_signal_to_dict(sig, news_summary, rationale, opt))
        except Exception as e:
            print(f"[scanner] Enrichment error {sig.ticker}: {e}")
            enriched.append(_signal_to_dict(sig))

    # Step 5: Update shared state
    _last_scan["signals"] = enriched
    _last_scan["macro"] = {
        "score": macro_score,
        "summary": macro_summary,
        "themes": [],
    }
    _last_scan["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _last_scan["total_scanned"] = len(raw_signals)

    print(f"[scanner] Done. {len(buys)} buys, {len(sells)} sells. Total: {time.time()-t0:.1f}s")
    return _last_scan


def get_last_scan() -> dict:
    return _last_scan


def scan_single(ticker: str) -> dict | None:
    """Scan a single ticker on demand."""
    macro_headlines = get_macro_news()
    macro_score, macro_summary = score_macro_sentiment(macro_headlines)
    df = get_ohlcv(ticker.upper(), period="6mo", interval="1d")
    if df is None or df.empty:
        return None
    sig = score_ticker(ticker.upper(), df, macro_score)
    if sig is None:
        return None
    headlines = get_ticker_news(ticker.upper(), days=3)
    _, news_summary = score_ticker_sentiment(ticker.upper(), headlines)
    rationale = generate_trade_rationale(
        ticker=sig.ticker, signal=sig.signal, confidence=sig.confidence,
        entry=sig.entry, stop_loss=sig.stop_loss, take_profit_1=sig.take_profit_1,
        strategy_reasons=sig.reasons, macro_summary=macro_summary,
        news_summary=news_summary,
    )
    opt = None
    if sig.signal in ("BUY", "SELL"):
        opt = play_to_dict(build_options_play(
            sig.ticker, sig.signal, sig.entry, sig.take_profit_1, sig.stop_loss))
    return _signal_to_dict(sig, news_summary, rationale, opt)
