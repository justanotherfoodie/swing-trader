"""Branch coverage for the 5 swing strategies.

Each branch decides both a direction and a score weight that is summed into the
BUY/SELL threshold, and each declares whether it is a fresh TRIGGER. Mislabelling a
state read as a trigger is what makes the scanner buy already-extended stocks.

Frames are built column-by-column so a single branch can be isolated - deriving them
from real indicator maths would activate several branches at once.
"""

import numpy as np
import pandas as pd
import pytest

from signals import strategies as st


def frame(n=80, **cols):
    """A frame with sane defaults for every column the strategies read."""
    base = {
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6,
        "rsi": 50.0, "support": 90.0, "resistance": 110.0,
        "ema9": 100.0, "ema21": 100.0, "ema50": 100.0, "ema200": 100.0,
        "macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0,
        "bb_upper": 105.0, "bb_middle": 100.0, "bb_lower": 95.0, "bb_width": 0.10,
        "vol_ratio": 1.0, "atr": 2.0,
    }
    df = pd.DataFrame({k: [v] * n for k, v in base.items()})
    for k, v in cols.items():
        if isinstance(v, (list, tuple)):        # values for the last len(v) bars
            df.loc[df.index[-len(v):], k] = list(v)
        else:
            df[k] = v
    return df


# ---------------------------------------------------------------- RSI mean reversion

def test_rsi_oversold_at_support_with_reversal_is_max_conviction():
    """RSI<30 + at support + reversal candle is the full-size 2.0 bullish trigger."""
    df = frame(rsi=25.0, close=[91.0, 91.0], support=90.0,
               open=[92.0, 92.0], low=[85.0, 85.0])
    r = st.rsi_mean_reversion(df)
    assert r.signal == 1 and r.score == 2.0 and r.is_trigger is True


def test_rsi_oversold_away_from_support_scores_lower():
    """Oversold without the structural support level is a weaker 1.2 read."""
    r = st.rsi_mean_reversion(frame(rsi=25.0, close=105.0, support=90.0))
    assert r.signal == 1 and r.score == 1.2


def test_falling_knife_filter_blocks_oversold_in_a_downtrend():
    """Oversold in a firm downtrend with no reversal candle scores ZERO.

    Buying that dip is the classic account-killer - the filter must hold."""
    df = frame(rsi=20.0, close=90.0, ema50=90.0, ema200=100.0,
               open=[89.0, 89.0], low=[88.9, 88.9], high=[90.1, 90.1])
    r = st.rsi_mean_reversion(df)
    assert r.score == 0 and r.signal == 0
    assert "avoid knife" in r.reason


def test_counter_trend_bounce_is_sized_down():
    """With a reversal candle in a downtrend the score is cut to 60%."""
    df = frame(rsi=25.0, close=[95.0, 95.0], support=94.0, ema50=90.0, ema200=100.0,
               open=[96.0, 96.0], low=[88.0, 88.0])
    r = st.rsi_mean_reversion(df)
    assert r.signal == 1 and r.score == pytest.approx(2.0 * 0.6)


def test_rsi_30_to_35_zone_is_only_a_trigger_with_a_reversal_candle():
    """The shallow oversold zone needs candle confirmation to count as actionable."""
    plain = st.rsi_mean_reversion(frame(rsi=32.0))
    assert plain.score == 1.0 and plain.is_trigger is False


def test_rsi_overbought_at_resistance_is_a_full_sell_trigger():
    """RSI>70 into resistance is the -2.0 bearish trigger."""
    r = st.rsi_mean_reversion(frame(rsi=75.0, close=110.0, resistance=110.0))
    assert r.signal == -1 and r.score == -2.0 and r.is_trigger is True


def test_rsi_65_to_70_is_context_not_a_trigger():
    """'Entering overbought' adds bearish weight but must never generate a SELL alone."""
    r = st.rsi_mean_reversion(frame(rsi=68.0))
    assert r.signal == -1 and r.score == -1.0 and r.is_trigger is False


def test_rsi_neutral_zone_scores_zero():
    """RSI 50 is no information - it must not tilt the net score either way."""
    assert st.rsi_mean_reversion(frame(rsi=50.0)).score == 0


def test_rsi_strategy_needs_the_rsi_column():
    """A frame without indicators degrades to 'Insufficient data', never raises."""
    df = pd.DataFrame({"close": [1.0] * 30})
    assert st.rsi_mean_reversion(df).reason == "Insufficient data"


# ---------------------------------------------------------------- MACD + RSI

def test_macd_bull_cross_with_room_to_run():
    """A fresh bullish cross with RSI<55 is the 2.0 conviction case."""
    df = frame(macd=[-0.1, 0.2], macd_signal=[0.0, 0.0], rsi=45.0)
    r = st.macd_rsi_confluence(df)
    assert r.signal == 1 and r.score == 2.0 and r.is_trigger is True


def test_macd_bull_cross_when_extended_is_heavily_discounted():
    """The same cross with RSI>65 is worth only 0.5 - the move is already made."""
    df = frame(macd=[-0.1, 0.2], macd_signal=[0.0, 0.0], rsi=70.0)
    assert st.macd_rsi_confluence(df).score == 0.5


def test_macd_bear_cross_from_overbought_is_full_size():
    """A bearish cross with RSI>65 is the -2.0 short trigger."""
    df = frame(macd=[0.1, -0.2], macd_signal=[0.0, 0.0], rsi=70.0)
    r = st.macd_rsi_confluence(df)
    assert r.signal == -1 and r.score == -2.0 and r.is_trigger is True


def test_macd_histogram_momentum_is_context_only():
    """Rising histogram adds conviction but is explicitly NOT a fresh trigger.

    If it were, the scanner could issue a BUY with no actual event."""
    df = frame(macd=0.5, macd_signal=0.2, macd_hist=[0.2, 0.3], rsi=50.0)
    r = st.macd_rsi_confluence(df)
    assert r.score == 0.6 and r.is_trigger is False


def test_macd_no_crossover_scores_zero():
    """Steady state on both sides of the signal line contributes nothing."""
    df = frame(macd=-0.5, macd_signal=-0.2, macd_hist=-0.3)
    assert st.macd_rsi_confluence(df).score == 0


# ---------------------------------------------------------------- EMA crossover

def test_ema_golden_cross_above_50_is_full_size():
    """9/21 cross with price above the 50 EMA is the 2.0 momentum trigger."""
    df = frame(ema9=[99.0, 101.0], ema21=[100.0, 100.0], ema50=95.0, close=100.0)
    r = st.ema_crossover(df)
    assert r.signal == 1 and r.score == 2.0 and r.is_trigger is True


def test_ema_cross_below_the_50_is_sized_down():
    """The same cross under the 50 EMA is a weaker 1.5 - it fights the bigger trend."""
    df = frame(ema9=[99.0, 101.0], ema21=[100.0, 100.0], ema50=105.0, close=100.0)
    assert st.ema_crossover(df).score == 1.5


def test_ema_death_cross_below_50_is_full_size_bearish():
    """9 crossing below 21 with price under the 50 EMA is the -2.0 trigger."""
    df = frame(ema9=[101.0, 99.0], ema21=[100.0, 100.0], ema50=105.0, close=100.0)
    r = st.ema_crossover(df)
    assert r.signal == -1 and r.score == -2.0 and r.is_trigger is True


def test_ema_continuation_is_context_not_a_trigger():
    """'Uptrend intact' with no fresh cross is 0.5 of context only.

    This is the exact case the trigger gate exists to stop from becoming a BUY."""
    df = frame(ema9=102.0, ema21=100.0, ema50=95.0, close=100.0)
    r = st.ema_crossover(df)
    assert r.score == 0.5 and r.is_trigger is False


# ---------------------------------------------------------------- BB squeeze

def test_squeeze_breakout_on_volume_is_a_full_trigger():
    """Squeeze + close above the upper band + >1.4x volume is the 2.0 breakout."""
    df = frame(bb_width=0.05, close=[100.0, 106.0], bb_upper=105.0, vol_ratio=2.0)
    r = st.bb_squeeze_breakout(df)
    assert r.signal == 1 and r.score == 2.0 and r.is_trigger is True


def test_squeeze_breakout_without_volume_is_half_weight():
    """No volume confirmation halves the breakout to 1.0 - most fail without it."""
    df = frame(bb_width=0.05, close=[100.0, 106.0], bb_upper=105.0, vol_ratio=1.0)
    r = st.bb_squeeze_breakout(df)
    assert r.score == 1.0 and r.is_trigger is True


def test_squeeze_breakdown_on_volume_is_bearish():
    """A break below the lower band on volume is the -2.0 short trigger."""
    df = frame(bb_width=0.05, close=[100.0, 94.0], bb_lower=95.0, vol_ratio=2.0)
    r = st.bb_squeeze_breakout(df)
    assert r.signal == -1 and r.score == -2.0


def test_squeeze_without_a_break_is_a_watch_only():
    """An active squeeze scores 0.3 with signal 0 - a coil, not a direction."""
    r = st.bb_squeeze_breakout(frame(bb_width=0.05))
    assert r.signal == 0 and r.score == 0.3 and r.is_trigger is False


def test_no_squeeze_scores_zero():
    """Wide bands relative to the 60-bar minimum mean no setup at all."""
    df = frame(bb_width=0.10)
    df.loc[df.index[-1], "bb_width"] = 0.50
    assert st.bb_squeeze_breakout(df).score == 0


def test_bb_strategy_handles_missing_columns():
    """Missing BB columns must degrade gracefully, not raise mid-scan."""
    df = pd.DataFrame({"close": [1.0] * 30})
    assert st.bb_squeeze_breakout(df).reason == "Insufficient data"


# ---------------------------------------------------------------- EMA50 + BB

def test_fresh_bounce_off_the_50_ema_in_an_uptrend():
    """Today tags the 50 EMA and closes above it, after a bar that did not - 2.0 trigger."""
    df = frame(low=[105.0, 100.5], close=[106.0, 102.0], ema50=100.0, ema200=95.0,
               bb_middle=101.0, rsi=50.0)
    r = st.ema50_bb_trend(df)
    assert r.signal == 1 and r.score == 2.0 and r.is_trigger is True


def test_sitting_on_the_50_ema_is_a_weaker_trigger():
    """Already sitting on support (previous bar also tagged it) scores 1.0, not 2.0."""
    df = frame(low=[100.5, 100.5], close=[102.0, 102.0], ema50=100.0, ema200=95.0,
               bb_middle=101.0)
    r = st.ema50_bb_trend(df)
    assert r.score == 1.0 and r.is_trigger is True


def test_riding_the_upper_band_is_context_only():
    """Strong-trend context adds 0.5 but is not an entry - chasing here is the trap."""
    df = frame(low=[110.0, 110.0], close=[112.0, 112.0], ema50=100.0, ema200=95.0,
               bb_middle=105.0, bb_upper=111.0)
    r = st.ema50_bb_trend(df)
    assert r.score == 0.5 and r.is_trigger is False


def test_downtrend_below_the_bb_midline_is_bearish_context():
    """50 below 200 and price under the midline is a -1.0 bearish state read."""
    df = frame(close=90.0, low=89.0, ema50=95.0, ema200=100.0, bb_middle=95.0)
    r = st.ema50_bb_trend(df)
    assert r.signal == -1 and r.score == -1.0 and r.is_trigger is False


def test_ema50_bb_needs_55_bars():
    """Under 55 bars there is no valid 200 EMA - the strategy must abstain."""
    assert st.ema50_bb_trend(frame(n=40)).reason == "Insufficient data"


def test_every_strategy_tolerates_an_empty_frame():
    """An empty DataFrame (delisted/halted ticker) must never crash the scanner."""
    empty = pd.DataFrame()
    for fn in st.ALL_STRATEGIES:
        r = fn(empty)
        assert r.score == 0 and r.signal == 0
