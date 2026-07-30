"""Scorer: trigger gating, confidence, stop/target maths, disabled strategies.

This is the module that turns indicator readings into a BUY the user acts on. The
trigger gate is the core safety property: score alone must never produce a trade.
"""

import pandas as pd
import pytest

from conftest import make_ohlcv
from signals import scorer
from signals import strategies as st
from signals.strategies import StrategyResult


def fixed(*results):
    """Force the scorer to see exactly these strategy results."""
    return lambda: [(lambda df, r=r: r) for r in results]


def bull(score=3.0, trigger=True, name="S1"):
    return StrategyResult(name, 1, score, "bullish", is_trigger=trigger)


def bear(score=-3.0, trigger=True, name="S2"):
    return StrategyResult(name, -1, score, "bearish", is_trigger=trigger)


@pytest.fixture
def df80():
    """80 bars with indicators already computed - long enough to pass the 55-bar gate."""
    from signals.indicators import add_all_indicators
    return add_all_indicators(make_ohlcv([100 + i * 0.2 for i in range(80)]))


def score(df, monkeypatch, results, macro=0.0):
    monkeypatch.setattr(scorer, "active_strategies", fixed(*results))
    return scorer.score_ticker("TEST", df, macro, indicators_ready=True)


# ---------------------------------------------------------------- trigger gating

def test_buy_requires_both_score_and_a_fresh_trigger(df80, monkeypatch):
    """Enough bullish score WITH a trigger produces a BUY."""
    s = score(df80, monkeypatch, [bull(3.0, True)])
    assert s.signal == "BUY" and s.triggers == ["S1"]


def test_score_without_a_trigger_is_only_a_watch(df80, monkeypatch):
    """Overwhelming bullish score but no fresh event must stay WATCH.

    This is the fix for flagging already-extended stocks that merely trend up; a
    regression here reinstates buying tops."""
    s = score(df80, monkeypatch, [bull(6.0, False)])
    assert s.signal == "WATCH"
    assert s.total_score == 6.0


def test_trigger_without_enough_score_is_a_watch(df80, monkeypatch):
    """A lone weak trigger below the +2.0 threshold is not tradeable either."""
    s = score(df80, monkeypatch, [bull(1.5, True)])
    assert s.signal == "WATCH"


def test_threshold_is_inclusive_at_exactly_two(df80, monkeypatch):
    """net == +2.0 with a trigger is a BUY - the boundary must not be off by one tick."""
    assert score(df80, monkeypatch, [bull(2.0, True)]).signal == "BUY"


def test_sell_needs_a_bearish_trigger_not_a_bullish_one(df80, monkeypatch):
    """A bearish net score with only a BULLISH trigger cannot become a SELL.

    Crossing the wires here would short stocks on bullish events."""
    s = score(df80, monkeypatch, [bear(-4.0, False), bull(0.5, True)])
    assert s.total_score == -3.5
    assert s.signal == "WATCH"


def test_macro_sentiment_can_push_a_signal_over_the_line(df80, monkeypatch):
    """Macro adds to the net score, so a borderline trigger can become a BUY."""
    assert score(df80, monkeypatch, [bull(1.5, True)], macro=1.0).signal == "BUY"


def test_macro_headwind_can_veto_a_borderline_signal(df80, monkeypatch):
    """Negative macro subtracts, keeping a marginal bullish setup on the sidelines."""
    assert score(df80, monkeypatch, [bull(2.5, True)], macro=-1.0).signal == "WATCH"


def test_conflicting_strategies_net_out(df80, monkeypatch):
    """Opposing strategies cancel; disagreement must not produce a confident trade."""
    s = score(df80, monkeypatch, [bull(3.0, True), bear(-3.0, True)])
    assert s.total_score == 0.0 and s.signal == "WATCH"


def test_a_raising_strategy_does_not_abort_the_scan(df80, monkeypatch):
    """One broken strategy is skipped; the rest still produce a verdict.

    A scan that dies on one ticker's bad data leaves the user with no signals at all."""
    def boom(df):
        raise ValueError("bad data")
    monkeypatch.setattr(scorer, "active_strategies",
                        lambda: [boom, lambda df: bull(3.0, True)])
    s = scorer.score_ticker("TEST", df80, 0.0, indicators_ready=True)
    assert s is not None and s.signal == "BUY"


# ---------------------------------------------------------------- confidence

def test_confidence_is_zero_with_no_conviction():
    """No directional evidence at all must give 0, not a floor value."""
    assert scorer._confidence(0, 0, 0, 0) == 0


def test_confidence_is_halved_without_a_trigger():
    """No fresh trigger halves confidence - the read is soft by definition."""
    with_t = scorer._confidence(6, 0, 0, 1)
    without = scorer._confidence(6, 0, 0, 0)
    assert without == int(round(with_t * 0.5))


def test_confidence_penalises_conflict():
    """Same gross conviction, but split between sides, scores materially lower.

    Confidence must reflect agreement, not just raw magnitude."""
    aligned = scorer._confidence(6, 0, 0, 1)
    conflicted = scorer._confidence(4, 2, 0, 1)
    assert aligned > conflicted


def test_confidence_rewards_multiple_triggers():
    """Two independent confirmations add a bonus over a single trigger."""
    assert scorer._confidence(4, 0, 0, 2) > scorer._confidence(4, 0, 0, 1)


def test_confidence_is_capped_at_100():
    """Extreme inputs must never produce an out-of-range confidence."""
    assert scorer._confidence(50, 0, 5, 5) == 100


def test_macro_folds_into_the_opposing_pool():
    """Bearish macro against a bullish book reduces purity, hence confidence."""
    assert scorer._confidence(6, 0, -3, 1) < scorer._confidence(6, 0, 0, 1)


# ---------------------------------------------------------------- levels

def test_long_stop_is_1_5_atr_below_entry(df80, monkeypatch):
    """The stop is ATR-scaled, so volatile names get proportionally wider stops.

    A fixed-dollar stop gets hit by ordinary noise on a volatile ticker."""
    s = score(df80, monkeypatch, [bull(3.0, True)])
    atr = float(df80["atr"].iloc[-1])
    assert s.stop_loss == pytest.approx(round(s.entry - atr * 1.5, 2))
    assert s.risk_per_share == pytest.approx(round(s.entry - s.stop_loss, 2))


def test_short_levels_are_mirrored(df80, monkeypatch):
    """For a SELL the stop sits ABOVE entry and targets below it."""
    s = score(df80, monkeypatch, [bear(-3.0, True)])
    assert s.signal == "SELL"
    assert s.stop_loss > s.entry
    assert s.take_profit_1 < s.entry and s.take_profit_2 < s.take_profit_1


def test_long_tp1_is_capped_at_resistance(df80, monkeypatch):
    """TP1 never advertises a target beyond the nearest resistance.

    An unreachable target makes the R:R look better than the trade really is."""
    s = score(df80, monkeypatch, [bull(3.0, True)])
    if s.target_note.startswith("TP1 capped"):
        assert s.take_profit_1 == pytest.approx(s.resistance)
    assert s.take_profit_1 <= max(s.resistance, s.entry + s.risk_per_share * 2) + 0.01


def test_risk_reward_and_share_sizing(df80, monkeypatch):
    """R:R and share count are derived from risk-per-share, not guessed.

    Position size = risk budget / risk per share is the whole sizing contract."""
    s = score(df80, monkeypatch, [bull(3.0, True)], macro=0.0)
    assert s.risk_reward == pytest.approx(
        round(abs(s.take_profit_1 - s.entry) / s.risk_per_share, 2))
    assert s.shares == int(scorer.DEFAULT_RISK_BUDGET / s.risk_per_share)
    assert s.position_value == pytest.approx(round(s.shares * s.entry, 2))


def test_poor_risk_reward_is_flagged_on_a_live_signal(df80, monkeypatch):
    """Sub-1.5 R:R on an actionable signal is called out in the target note."""
    s = score(df80, monkeypatch, [bull(3.0, True)])
    if s.risk_reward < scorer.MIN_RR:
        assert "Poor R:R" in s.target_note


def test_quality_grade_requires_confidence_and_rr(df80, monkeypatch):
    """'high' quality demands confidence >=70, R:R >=2 and a trigger together."""
    s = score(df80, monkeypatch, [bull(3.0, True)])
    if s.quality == "high":
        assert s.confidence >= 70 and s.risk_reward >= 2.0 and s.triggers
    assert s.quality in ("high", "medium", "low")


def test_holding_window_shortens_with_conviction(df80, monkeypatch):
    """A strong net score implies a faster move, hence a shorter stated hold."""
    strong = score(df80, monkeypatch, [bull(5.0, True)])
    assert strong.holding_days == "3-5 days"
    weak = score(df80, monkeypatch, [bull(0.5, False)])
    assert weak.holding_days == "Monitor - no fresh trigger yet"


# ---------------------------------------------------------------- guards

def test_too_few_bars_returns_none(df80):
    """<55 bars cannot support a 200 EMA - return None rather than a bad signal."""
    assert scorer.score_ticker("TEST", df80.head(50), 0.0) is None


def test_none_frame_returns_none():
    """A failed download (None) must not raise inside the scan loop."""
    assert scorer.score_ticker("TEST", None, 0.0) is None


def test_missing_indicator_columns_fall_back_to_defaults(df80, monkeypatch):
    """Without an 'atr' column the stop falls back to 2% of price rather than crashing."""
    raw = df80[["open", "high", "low", "close", "volume"]].copy()
    monkeypatch.setattr(scorer, "active_strategies", fixed(bull(3.0, True)))
    s = scorer.score_ticker("TEST", raw, 0.0, indicators_ready=True)
    close = float(raw["close"].iloc[-1])
    assert s.atr == pytest.approx(round(close * 0.02, 2))
    assert s.rsi == 50.0


# ---------------------------------------------------------------- strategy roster

def test_disabled_strategy_is_excluded_from_scoring():
    """MACD+RSI Confluence is disabled on walk-forward evidence and must not run.

    Silently re-enabling it reintroduces a strategy measured as losing money."""
    names = [f.__name__ for f in scorer.active_strategies()]
    assert "macd_rsi_confluence" in scorer.DISABLED_STRATEGIES
    assert "macd_rsi_confluence" not in names
    assert len(names) == len(st.ALL_STRATEGIES) - 1


def test_active_strategies_honours_the_disabled_set(monkeypatch):
    """The roster is driven by DISABLED_STRATEGIES, so the switch actually works."""
    monkeypatch.setattr(scorer, "DISABLED_STRATEGIES", set())
    assert len(scorer.active_strategies()) == len(st.ALL_STRATEGIES)
    monkeypatch.setattr(scorer, "DISABLED_STRATEGIES",
                        {"macd_rsi_confluence", "ema_crossover"})
    assert len(scorer.active_strategies()) == len(st.ALL_STRATEGIES) - 2


def test_real_strategies_run_end_to_end_offline(df80):
    """The whole pipeline works on a synthetic frame with no network access."""
    s = scorer.score_ticker("TEST", df80, 0.0)
    assert s is not None
    assert s.signal in ("BUY", "SELL", "WATCH")
    assert len(s.strategy_results) == len(scorer.active_strategies())
