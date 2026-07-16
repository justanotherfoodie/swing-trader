"""
AI analyzer — sentiment scoring and trade rationale generation via LLM API.
"""

import os, json
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

_client = None

def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def score_macro_sentiment(headlines: list[dict]) -> tuple[float, str]:
    """
    Score macro news sentiment: returns (score, summary).
    score: -2.0 (very bearish) to +2.0 (very bullish)
    """
    if not headlines:
        return 0.0, "No macro news available."

    titles = "\n".join(f"- {h['title']}" for h in headlines[:12])

    prompt = f"""You are a macro market analyst. Analyze these news headlines and return a JSON object.

Headlines:
{titles}

Return ONLY valid JSON in this exact format:
{{
  "score": <float between -2.0 and 2.0>,
  "sentiment": "<very_bearish | bearish | neutral | bullish | very_bullish>",
  "summary": "<2-3 sentence summary of macro conditions and key risks or tailwinds>",
  "key_themes": ["<theme1>", "<theme2>", "<theme3>"]
}}

Scoring guide:
-2.0 = severe macro headwinds (rate hike shock, recession confirmed, systemic crisis)
-1.0 = bearish (tightening, slowdown signs, elevated uncertainty)
 0.0 = neutral / mixed signals
+1.0 = mildly bullish (rate pause, soft landing, stable growth)
+2.0 = very bullish (rate cuts, strong GDP, risk-on environment)"""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        return float(data["score"]), data.get("summary", "")
    except Exception as e:
        if "authentication" not in str(e).lower():
            print(f"[analyzer] Macro sentiment error: {e}")
        return 0.0, "Unable to analyze macro conditions."


def score_ticker_sentiment(ticker: str, headlines: list[dict]) -> tuple[float, str]:
    """Score ticker-specific news sentiment: -1.0 to +1.0."""
    if not headlines:
        return 0.0, "No recent news."

    titles = "\n".join(f"- {h['title']}" for h in headlines[:8])

    prompt = f"""Analyze these news headlines for stock {ticker} and return JSON only.

Headlines:
{titles}

Return ONLY:
{{
  "score": <float between -1.0 and 1.0>,
  "summary": "<1-2 sentence assessment of news impact on short-term price>"
}}"""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        return float(data["score"]), data.get("summary", "")
    except Exception as e:
        if "authentication" not in str(e).lower():
            print(f"[analyzer] Ticker sentiment error {ticker}: {e}")
        return 0.0, "Unable to analyze news."


def generate_trade_rationale(
    ticker: str,
    signal: str,
    confidence: int,
    entry: float,
    stop_loss: float,
    take_profit_1: float,
    strategy_reasons: list[str],
    macro_summary: str,
    news_summary: str,
) -> str:
    """Generate a concise trade rationale paragraph."""
    reasons_text = "\n".join(f"- {r}" for r in strategy_reasons)

    prompt = f"""You are a professional swing trader. Write a concise 3-4 sentence trade rationale.

Trade: {signal} {ticker}
Confidence: {confidence}/100
Entry: ${entry} | Stop: ${stop_loss} | Target 1: ${take_profit_1}

Technical signals:
{reasons_text}

Macro context: {macro_summary}
Stock news: {news_summary}

Write a direct, professional rationale covering: why this trade, key risk, and what to watch.
No bullet points. Plain paragraph. Under 80 words."""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        if "authentication" not in str(e).lower():
            print(f"[analyzer] Rationale error {ticker}: {e}")
        reasons = "; ".join(strategy_reasons[:2]) if strategy_reasons else "technical confluence"
        return f"{signal} signal on {ticker} based on {reasons}. Entry at ${entry}, stop at ${stop_loss}."
