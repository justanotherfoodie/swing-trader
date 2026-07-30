"""Exit-rule tests for portfolio._evaluate_one - the profit ladder and stop logic.

These rules decide when real money comes off the table. A wrong branch here either
holds a losing position past its stop or dumps a winner early, so every branch and
its priority ordering is pinned.
"""

import pytest

from conftest import make_position
import portfolio as pf


def ev(state, mark=None, spot=None, **over):
    if mark is not None:
        state["mark"] = mark
    if spot is not None:
        state["spot"] = spot
    return pf._evaluate_one(make_position(**over))


# ---------------------------------------------------------------- stop rules

def test_stop_fires_when_underlying_breaks_stop(eval_env):
    """A call whose underlying trades at/below the stop must SELL immediately.

    This is the hard risk limit; if it fails to fire the position can run to zero."""
    r = ev(eval_env, mark=4.5, spot=95.0)
    assert r["action"] == "SELL" and r["urgency"] == "now"
    assert "stop level" in r["reason"]


def test_put_stop_uses_opposite_direction(eval_env):
    """For a put, the stop is breached when spot rises ABOVE it.

    A sign error here would leave every bearish position with no working stop."""
    r = ev(eval_env, mark=4.0, spot=106.0, kind="put",
           stop=105.0, target=90.0, long_strike=100.0)
    assert r["action"] == "SELL"
    assert "stop level" in r["reason"]


def test_premium_loss_stop_at_40pct(eval_env):
    """Losing >=40% of premium exits even if the underlying never hit the stop.

    Long options bleed via theta/IV without the stock moving; without this rule the
    position dies quietly."""
    r = ev(eval_env, mark=3.0, spot=99.0)   # 5.00 -> 3.00 = -40%
    assert r["action"] == "SELL"
    assert "Down" in r["reason"]


def test_small_loss_inside_stop_holds(eval_env):
    """A -20% drawdown is inside the stop and must stay open.

    Cutting every wobble converts a 40% stop into a 20% one and guarantees churn."""
    r = ev(eval_env, mark=4.0, spot=99.0)
    assert r["action"] == "HOLD"
    assert r["sell_contracts"] == 0


# ---------------------------------------------------------------- profit ladder

def test_tier1_scales_out_half(eval_env):
    """At +25% with >=2 contracts, sell half and keep the rest.

    This is the rung that converts paper gains into cash; selling everything or
    nothing both defeat the ladder."""
    r = ev(eval_env, mark=6.25, spot=104.0, contracts=4)
    assert r["action"] == "SCALE"
    assert r["sell_contracts"] == 2


def test_tier2_takes_the_bulk(eval_env):
    """At +50% tier 2 sells the majority (ceil of half), leaving a runner."""
    r = ev(eval_env, mark=7.5, spot=106.0, contracts=3, peak_pnl_pct=50.0)
    assert r["action"] == "SCALE"
    assert r["sell_contracts"] == 2   # (3+1)//2


def test_tier3_closes_the_runner(eval_env):
    """At +80% the whole position closes - no partial, no runner.

    Beyond this the expected value of holding is dominated by giveback risk."""
    r = ev(eval_env, mark=9.5, spot=112.0, contracts=4, peak_pnl_pct=90.0)
    assert r["action"] == "SELL"
    assert r["sell_contracts"] == 4


def test_single_contract_cannot_scale_takes_whole_profit_at_30(eval_env):
    """One contract is indivisible: at +30% it exits fully rather than SCALE.

    Emitting SCALE on 1 contract would produce an unfillable instruction."""
    r = ev(eval_env, mark=6.6, spot=105.0, contracts=1)
    assert r["action"] == "SELL"
    assert r["sell_contracts"] == 1


def test_single_contract_below_30_holds(eval_env):
    """A 1-contract position up 25% must HOLD - the 25% rung does not apply to it."""
    r = ev(eval_env, mark=6.25, spot=104.0, contracts=1)
    assert r["action"] == "HOLD"


def test_taken_rung_does_not_fire_again(eval_env):
    """A tier already recorded in scaled_out must not re-trigger at the same level.

    Otherwise the ladder would keep selling the remainder on every refresh."""
    r = ev(eval_env, mark=6.25, spot=104.0, contracts=2, scaled_out=["t1"])
    assert r["action"] == "HOLD"


# ---------------------------------------------------------------- trailing lock

def test_trailing_exit_after_giveback(eval_env):
    """Peaked +60%, now +30%: giveback exceeds max(10, 35% of peak)=21 -> SELL.

    This is the rule that stops a winner round-tripping to breakeven."""
    r = ev(eval_env, mark=6.5, spot=104.0, contracts=4, peak_pnl_pct=60.0)
    assert r["action"] == "SELL"
    assert "giving the gain" in r["reason"]


def test_trailing_not_armed_below_20pct_peak(eval_env):
    """A peak under +20% never arms the trail; small peaks are noise, not gains."""
    r = ev(eval_env, mark=5.0, spot=101.0, contracts=4, peak_pnl_pct=15.0)
    assert r["action"] == "HOLD"


def test_trail_exit_level_scales_with_peak():
    """Giveback is ~a third of the peak with a 10-point floor.

    A flat giveback is brutal on small peaks and far too loose on large ones."""
    assert pf.trail_exit_level(20.0) == pytest.approx(10.0)   # floor applies
    assert pf.trail_exit_level(100.0) == pytest.approx(65.0)  # 35% of peak


def test_peak_is_high_water_mark_not_current(eval_env):
    """peak_pnl_pct only ever ratchets up, so the trail cannot be reset by a dip."""
    r = ev(eval_env, mark=5.5, spot=102.0, contracts=4, peak_pnl_pct=45.0)
    assert r["peak_pnl_pct"] == 45.0


# ---------------------------------------------------------------- time / target

def test_time_stop_near_expiry(eval_env):
    """<=14 DTE closes regardless of P&L - gamma/theta risk dominates there."""
    from datetime import datetime, timedelta, timezone
    exp = (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()
    r = ev(eval_env, mark=5.2, spot=101.0, contracts=4, expiry=exp)
    assert r["action"] == "SELL"
    assert "days to expiry" in r["reason"]


def test_expiry_today_is_still_a_sell_not_a_crash(eval_env):
    """Expiry == today (dte 0) must produce a verdict, not an exception.

    An unhandled edge on expiry day is exactly when the user needs the answer."""
    from datetime import datetime, timezone
    exp = datetime.now(timezone.utc).date().isoformat()
    r = ev(eval_env, mark=5.1, spot=101.0, contracts=4, expiry=exp)
    assert r["dte"] == 0
    assert r["action"] == "SELL"


def test_max_hold_days_recycles_capital(eval_env):
    """Held >= 12 days exits: the swing thesis has a shelf life."""
    from datetime import datetime, timedelta, timezone
    opened = (datetime.now(timezone.utc) - timedelta(days=13)).isoformat()
    r = ev(eval_env, mark=5.1, spot=101.0, contracts=4, opened_at=opened)
    assert r["action"] == "SELL"
    assert "past the swing window" in r["reason"]


def test_price_target_reached_locks_in(eval_env):
    """Underlying reaching the target sells even when the mark lags.

    The thesis is complete; holding past it is a new trade nobody decided to make."""
    r = ev(eval_env, mark=5.2, spot=116.0, contracts=4)
    assert r["action"] == "SELL"
    assert "target" in r["reason"].lower()


def test_signal_flip_exits(eval_env, monkeypatch):
    """A flipped scanner signal breaks the thesis and exits the position."""
    import pandas as pd
    from types import SimpleNamespace
    eval_env["flip"] = SimpleNamespace(signal="SELL")
    # give _evaluate_one a non-empty frame so the flip branch is reached
    monkeypatch.setattr(pf, "get_ohlcv",
                        lambda *a, **k: pd.DataFrame({"close": [1.0, 2.0]}))
    r = ev(eval_env, mark=5.1, spot=101.0, contracts=4)
    assert r["action"] == "SELL"
    assert "flipped" in r["reason"]


# ---------------------------------------------------------------- priority order

def test_stop_beats_profit_ladder(eval_env):
    """When the underlying broke the stop, the stop wins even at a profit mark.

    Risk limits must never be outranked by an optimistic P&L reading."""
    r = ev(eval_env, mark=6.25, spot=95.0, contracts=4)
    assert r["action"] == "SELL"
    assert "stop level" in r["reason"]


def test_spread_max_profit_uses_strike_width(eval_env):
    """For a vertical, pct_of_max is measured against width - debit, not the target.

    Using the single-leg formula on a spread would overstate remaining upside."""
    r = ev(eval_env, mark=8.0, spot=104.0, contracts=1,
           long_strike=100.0, short_strike=110.0, net_debit=5.0,
           target=200.0, stop=1.0)
    # width 10 - debit 5 = 5 max profit; mark 8 => (8-5)/5 = 60% of max
    assert r["pct_of_max"] == 60
    assert r["structure"] == "spread"


def test_pnl_math_scales_with_contracts(eval_env):
    """Cost/value/P&L must all multiply by 100 * contracts.

    A missing multiplier misreports dollars by 100x and mis-sizes every decision."""
    r = ev(eval_env, mark=6.0, spot=102.0, contracts=3)
    assert r["cost"] == 1500.0
    assert r["value"] == 1800.0
    assert r["pnl"] == 300.0
    assert r["pnl_pct"] == 20.0


def test_zero_premium_position_does_not_divide_by_zero(eval_env):
    """A 0-debit position (bad data / free roll) must not raise ZeroDivisionError."""
    r = ev(eval_env, mark=0.0, spot=101.0, contracts=1, net_debit=0.0)
    assert r["pnl_pct"] == 0
    assert r["action"] in ("HOLD", "SELL", "SCALE")


def test_worthless_mark_is_a_stop(eval_env):
    """A mark that has gone to zero is a total loss and must fire the stop."""
    r = ev(eval_env, mark=0.0, spot=99.0, contracts=2)
    assert r["action"] == "SELL"
    assert r["pnl_pct"] == -100.0
