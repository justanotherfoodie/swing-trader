"""Indicator maths verified against hand-computed values on small fixed series.

Every strategy reads these columns. A silently wrong RSI or ATR does not raise - it
just produces confident, wrong trades, so the arithmetic is pinned numerically.
"""

import numpy as np
import pandas as pd
import pytest

from conftest import make_ohlcv
from signals import indicators as ind


def test_ema_matches_hand_computed_recursion():
    """EMA uses alpha = 2/(n+1) with adjust=False, seeded on the first value.

    A different seeding convention shifts every crossover by days."""
    s = pd.Series([1.0, 2.0, 3.0])
    out = ind._ema(s, 2)          # alpha = 2/3
    assert out.iloc[0] == pytest.approx(1.0)
    assert out.iloc[1] == pytest.approx(1.0 + (2 / 3) * (2 - 1))       # 1.6667
    assert out.iloc[2] == pytest.approx(out.iloc[1] + (2 / 3) * (3 - out.iloc[1]))


def test_rsi_is_100_when_price_only_rises():
    """Uninterrupted gains give an undefined average loss -> RSI should be 100, not NaN.

    NaN silently disables every RSI branch exactly when a stock is most extended."""
    s = pd.Series(np.arange(1.0, 30.0))
    assert ind._rsi(s, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_near_50_on_perfectly_symmetric_moves():
    """Equal-sized alternating up/down moves hover around the RSI 50 midpoint.

    A drift away from 50 on symmetric data would mean the gain/loss split is skewed."""
    s = pd.Series([100 + (1 if i % 2 else 0) for i in range(60)], dtype=float)
    assert ind._rsi(s, 14).iloc[-1] == pytest.approx(50.0, abs=3.0)


def test_rsi_hand_computed_wilder_value():
    """Wilder smoothing (com=length-1, seeded on the first valid delta) reproduces a
    hand-computed value.

    Guards against an accidental switch to a simple moving average, which would move
    the 30/70 crossings by several points and change every entry."""
    s = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0])
    # deltas: NaN, +1, +1, -1, +2 ; alpha = 1/3, seeded on the first valid delta
    ag, al = 1.0, 0.0
    for g, l in [(1, 0), (0, 1), (2, 0)]:
        ag += (g - ag) / 3
        al += (l - al) / 3
    expected = 100 - 100 / (1 + ag / al)
    assert ind._rsi(s, 3).iloc[-1] == pytest.approx(expected)
    assert expected == pytest.approx(83.333, abs=0.01)


def test_rsi_falls_below_30_after_a_sharp_selloff():
    """A sustained decline must actually reach the oversold zone the strategy watches."""
    s = pd.Series([100 * (0.98 ** i) for i in range(40)])
    assert ind._rsi(s, 14).iloc[-1] < 30


def test_macd_is_fast_minus_slow_ema_and_hist_is_the_residual():
    """MACD line, signal and histogram must be internally consistent.

    The crossover triggers compare line vs signal; an inconsistent histogram would
    make the momentum branch disagree with the crossover branch."""
    s = pd.Series(np.linspace(100, 140, 60))
    ml, sl, hist = ind._macd(s)
    assert ml.iloc[-1] == pytest.approx(
        ind._ema(s, 12).iloc[-1] - ind._ema(s, 26).iloc[-1])
    assert sl.iloc[-1] == pytest.approx(ind._ema(ml, 9).iloc[-1])
    assert hist.iloc[-1] == pytest.approx(ml.iloc[-1] - sl.iloc[-1])


def test_macd_positive_in_an_uptrend_negative_in_a_downtrend():
    """Sign of the MACD line must follow the direction of the series."""
    up = pd.Series(np.linspace(100, 200, 80))
    down = pd.Series(np.linspace(200, 100, 80))
    assert ind._macd(up)[0].iloc[-1] > 0
    assert ind._macd(down)[0].iloc[-1] < 0


def test_bollinger_bands_are_mean_plus_minus_two_sample_sigma():
    """Bands use the SAMPLE stdev (ddof=1) of the 20-bar window.

    Population stdev would make the bands ~2.5% tighter and fire spurious breakouts."""
    vals = list(np.linspace(100, 120, 25))
    s = pd.Series(vals)
    u, m, l, w = ind._bbands(s, 20, 2)
    window = s.iloc[-20:]
    assert m.iloc[-1] == pytest.approx(window.mean())
    assert u.iloc[-1] == pytest.approx(window.mean() + 2 * window.std(ddof=1))
    assert l.iloc[-1] == pytest.approx(window.mean() - 2 * window.std(ddof=1))
    assert w.iloc[-1] == pytest.approx((u.iloc[-1] - l.iloc[-1]) / m.iloc[-1])


def test_bollinger_width_is_zero_on_a_flat_series():
    """A perfectly flat series has zero width - the extreme 'squeeze' case."""
    s = pd.Series([100.0] * 30)
    _, _, _, w = ind._bbands(s, 20, 2)
    assert w.iloc[-1] == pytest.approx(0.0)


def test_atr_uses_true_range_including_gaps():
    """True range takes the max of H-L and both gap measures vs the previous close.

    Ignoring gaps understates ATR, which directly shrinks every stop distance."""
    df = make_ohlcv(closes=[100.0, 120.0], highs=[101.0, 121.0], lows=[99.0, 119.0])
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift(1)).abs(),
                    (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
    assert tr.iloc[1] == pytest.approx(21.0)     # 121 - 100 gap, not the 2.0 range
    atr = ind._atr(df["high"], df["low"], df["close"], 14)
    # first value seeds at TR[0]=2, then Wilder-smooths in the 21
    assert atr.iloc[-1] == pytest.approx(2.0 + (21.0 - 2.0) / 14)


def test_atr_is_positive_and_finite_on_real_shaped_data():
    """ATR must never be NaN at the last bar - the scorer multiplies it into the stop."""
    df = make_ohlcv([100 + i * 0.5 for i in range(60)])
    atr = ind._atr(df["high"], df["low"], df["close"], 14)
    assert atr.iloc[-1] > 0 and np.isfinite(atr.iloc[-1])


def test_add_all_indicators_produces_every_column_the_strategies_read():
    """A missing column silently downgrades strategies to 'Insufficient data'."""
    closes = [100 + i * 0.3 + (1.0 if i % 5 == 0 else 0) for i in range(80)]
    df = ind.add_all_indicators(make_ohlcv(closes))
    for col in ["ema9", "ema21", "ema50", "ema200", "rsi", "macd", "macd_signal",
                "macd_hist", "bb_upper", "bb_middle", "bb_lower", "bb_width",
                "atr", "vol_ratio", "support", "resistance"]:
        assert col in df.columns, col
    assert not df[["ema9", "rsi", "macd", "atr", "bb_upper"]].iloc[-1].isna().any()


def test_add_all_indicators_does_not_mutate_the_caller_frame():
    """The input frame is copied; mutating a cached OHLCV frame would leak state
    between tickers and backtest windows."""
    df = make_ohlcv([100.0] * 60)
    ind.add_all_indicators(df)
    assert "rsi" not in df.columns


def test_support_resistance_averages_the_five_extremes():
    """Support/resistance are the mean of the 5 lowest lows / 5 highest highs."""
    df = make_ohlcv([100 + i for i in range(60)])
    sup, res = ind.find_support_resistance(df)
    assert sup == pytest.approx(round(float(df["low"].nsmallest(5).mean()), 2))
    assert res == pytest.approx(round(float(df["high"].nlargest(5).mean()), 2))


def test_reversal_candle_detects_a_hammer():
    """A long lower wick with a small body is a hammer - the bounce confirmation."""
    df = make_ohlcv(closes=[100.0, 100.5], opens=[100.0, 100.0],
                    highs=[101.0, 101.0], lows=[99.0, 97.0])
    assert ind.is_reversal_candle(df) is True


def test_reversal_candle_rejects_an_ordinary_up_candle():
    """A plain green candle after a green candle is not a reversal signal."""
    df = make_ohlcv(closes=[100.0, 101.0], opens=[99.5, 100.5],
                    highs=[101.5, 101.2], lows=[99.4, 100.4])
    assert ind.is_reversal_candle(df) is False


def test_reversal_candle_on_a_single_bar_is_false():
    """Fewer than 2 bars cannot form a pattern - must return False, not raise."""
    assert ind.is_reversal_candle(make_ohlcv([100.0])) is False
