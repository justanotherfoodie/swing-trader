"""
Signal scorer - aggregates 5 strategies + macro into an actionable, risk-managed trade.

Design principles (what a discretionary swing trader actually checks):
  1. A BUY/SELL requires a fresh TRIGGER, not just "trend is up". Continuation/state
     reads add conviction but can never, alone, generate a signal.
  2. Confidence reflects real conviction: directional strength scaled by how much the
     strategies AGREE (conflict is penalized), not an arbitrary -12..+12 remap.
  3. Targets respect structure: TP1 is capped at the next resistance (for longs) so we
     never advertise an unreachable target, and trades with sub-1.5 R:R are downgraded.
  4. Position sizing is explicit: shares = risk_budget / risk_per_share.

Signal thresholds (net directional score):
  net >= +2.0 AND a bullish trigger present -> BUY
  net <= -2.0 AND a bearish trigger present -> SELL
  otherwise                                  -> WATCH
"""

from dataclasses import dataclass
import pandas as pd
from .indicators import add_all_indicators, find_support_resistance
from .strategies import ALL_STRATEGIES, StrategyResult

# Default risk per trade in account currency. Tune to your account size
# (1-2% of equity is the common rule; $200 ~= 1% of a $20k account).
DEFAULT_RISK_BUDGET = 200.0
NET_THRESHOLD = 2.0
MIN_RR = 1.5          # trades below this reward:risk are flagged low-quality
ATR_STOP_MULT = 1.5


@dataclass
class TradeSignal:
    ticker: str
    signal: str                   # BUY | SELL | WATCH
    confidence: int               # 0-100
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_per_share: float
    risk_reward: float            # R:R to TP1
    shares: int                   # for DEFAULT_RISK_BUDGET
    position_value: float         # shares * entry
    atr: float
    atr_pct: float                # ATR as % of price (volatility gauge)
    rsi: float
    strategy_results: list[StrategyResult]
    macro_score: float
    total_score: float
    triggers: list[str]           # names of strategies that fired a fresh trigger
    quality: str                  # "high" | "medium" | "low"
    target_note: str              # e.g. "TP1 capped at resistance"
    reasons: list[str]
    support: float
    resistance: float
    holding_days: str


def _confidence(bull: float, bear: float, macro: float, n_triggers: int) -> int:
    """Conviction-based confidence: strength scaled by agreement (purity)."""
    # Fold macro into the directional pools so disagreement is penalized too.
    if macro > 0:
        bull += macro
    elif macro < 0:
        bear += abs(macro)

    gross = bull + bear
    if gross == 0:
        return 0
    net = bull - bear
    purity = abs(net) / gross                      # 1.0 = all aligned, 0 = total conflict
    strength = min(1.0, max(bull, bear) / 6.0)     # 6 pts of aligned conviction = full
    conf = 70 * strength + 30 * purity
    if n_triggers == 0:
        conf *= 0.5                                # no fresh trigger = soft, halve it
    elif n_triggers >= 2:
        conf = min(100, conf + 8)                  # multiple confirmations
    return int(min(100, max(0, round(conf))))


def score_ticker(ticker: str, df: pd.DataFrame, macro_sentiment: float = 0.0,
                 risk_budget: float = DEFAULT_RISK_BUDGET) -> "TradeSignal | None":
    if df is None or len(df) < 55:
        return None

    try:
        df = add_all_indicators(df)
    except Exception as e:
        print(f"[scorer] Indicator error for {ticker}: {e}")
        return None

    results: list[StrategyResult] = []
    for strategy in ALL_STRATEGIES:
        try:
            results.append(strategy(df))
        except Exception as e:
            print(f"[scorer] Strategy error {strategy.__name__} {ticker}: {e}")

    bull = sum(r.score for r in results if r.score > 0)
    bear = sum(-r.score for r in results if r.score < 0)
    net  = bull - bear + macro_sentiment

    bull_triggers = [r for r in results if r.is_trigger and r.signal == 1]
    bear_triggers = [r for r in results if r.is_trigger and r.signal == -1]

    # Trigger-gated decision: direction needs both score AND a fresh event.
    if net >= NET_THRESHOLD and bull_triggers:
        signal = "BUY"
    elif net <= -NET_THRESHOLD and bear_triggers:
        signal = "SELL"
    else:
        signal = "WATCH"

    triggers = [r.name for r in (bull_triggers if signal == "BUY" else
                                 bear_triggers if signal == "SELL" else
                                 bull_triggers + bear_triggers)]

    confidence = _confidence(bull, bear, macro_sentiment, len(triggers))

    close   = float(df["close"].iloc[-1])
    atr     = float(df["atr"].iloc[-1]) if "atr" in df.columns else close * 0.02
    rsi_val = float(df["rsi"].iloc[-1]) if "rsi" in df.columns else 50.0
    sup, res = find_support_resistance(df)
    entry = round(close, 2)

    target_note = ""

    if signal == "SELL":
        stop_loss = round(entry + atr * ATR_STOP_MULT, 2)
        risk = stop_loss - entry
        raw_tp1 = entry - risk * 2
        raw_tp2 = entry - risk * 3
        # Cap target at support (a short cover target shouldn't blow past structure).
        if sup < entry and raw_tp1 < sup:
            take_profit_1 = round(max(raw_tp1, sup), 2)
            target_note = "TP1 set at support"
        else:
            take_profit_1 = round(raw_tp1, 2)
        take_profit_2 = round(raw_tp2, 2)
    else:  # BUY or WATCH both get long-side levels for reference
        stop_loss = round(entry - atr * ATR_STOP_MULT, 2)
        risk = entry - stop_loss
        raw_tp1 = entry + risk * 2
        raw_tp2 = entry + risk * 3
        # Cap TP1 at the nearest resistance so we don't advertise an unreachable target.
        if res > entry and raw_tp1 > res:
            take_profit_1 = round(res, 2)
            target_note = "TP1 capped at resistance"
        else:
            take_profit_1 = round(raw_tp1, 2)
        take_profit_2 = round(raw_tp2, 2)

    risk_per_share = round(abs(entry - stop_loss), 2)
    reward = abs(take_profit_1 - entry)
    risk_reward = round(reward / risk_per_share, 2) if risk_per_share > 0 else 0.0
    shares = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0
    position_value = round(shares * entry, 2)
    atr_pct = round(atr / close * 100, 2) if close else 0.0

    # Quality grade combines conviction, R:R, and volatility sanity.
    if confidence >= 70 and risk_reward >= 2.0 and len(triggers) >= 1:
        quality = "high"
    elif confidence >= 55 and risk_reward >= MIN_RR:
        quality = "medium"
    else:
        quality = "low"
    if risk_reward < MIN_RR and signal != "WATCH":
        target_note = (target_note + " | " if target_note else "") + f"Poor R:R {risk_reward}"

    reasons = [r.reason for r in results if r.score != 0]

    if abs(net) >= 4:
        holding_days = "3-5 days"
    elif signal != "WATCH":
        holding_days = "5-10 days"
    else:
        holding_days = "Monitor - no fresh trigger yet"

    return TradeSignal(
        ticker=ticker,
        signal=signal,
        confidence=confidence,
        entry=entry,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        risk_per_share=risk_per_share,
        risk_reward=risk_reward,
        shares=shares,
        position_value=position_value,
        atr=round(atr, 2),
        atr_pct=atr_pct,
        rsi=round(rsi_val, 1),
        strategy_results=results,
        macro_score=round(macro_sentiment, 2),
        total_score=round(net, 2),
        triggers=triggers,
        quality=quality,
        target_note=target_note,
        reasons=reasons,
        support=sup,
        resistance=res,
        holding_days=holding_days,
    )
