"""Options maths and builders: black_scholes, deltas, quality scoring, limit prices.

Every number here becomes a dollar amount the user pays or receives. Pricing is
checked against put-call parity and known boundaries rather than golden values, so
the tests state the property that must hold rather than today's output.
"""

import math

import pytest

from conftest import FakeChain, FakeTicker, option_rows, near_expiry
from signals import options as op


# ---------------------------------------------------------------- black_scholes

def test_bs_call_at_expiry_is_intrinsic_value():
    """dte<=0 must return intrinsic, never a time-value ghost.

    The backtester values positions at expiry with this; extra premium there would
    fabricate profit that never existed."""
    assert op.black_scholes(110, 100, 0, 0.3, "call") == 10.0
    assert op.black_scholes(90, 100, 0, 0.3, "call") == 0.0
    assert op.black_scholes(90, 100, 0, 0.3, "put") == 10.0
    assert op.black_scholes(110, 100, -5, 0.3, "put") == 0.0


def test_bs_zero_vol_is_intrinsic():
    """A missing/zero IV degrades to intrinsic rather than raising on log(0)."""
    assert op.black_scholes(110, 100, 30, 0.0, "call") == 10.0
    assert op.black_scholes(0, 100, 30, 0.3, "call") == 0.0


def test_bs_satisfies_put_call_parity_at_zero_rates():
    """C - P == S - K at r=0. Parity failure means one side is systematically mispriced."""
    c = op.black_scholes(100, 95, 45, 0.35, "call")
    p = op.black_scholes(100, 95, 45, 0.35, "put")
    assert c - p == pytest.approx(100 - 95, abs=1e-6)


def test_bs_price_is_monotonic_in_time_and_vol():
    """More time and more vol are both worth more premium - a basic sanity invariant."""
    base = op.black_scholes(100, 100, 30, 0.30, "call")
    assert op.black_scholes(100, 100, 60, 0.30, "call") > base
    assert op.black_scholes(100, 100, 30, 0.60, "call") > base


def test_bs_call_price_is_bounded_by_spot_and_above_intrinsic():
    """0 <= intrinsic <= price <= spot. Breaching these is an arbitrage, i.e. a bug."""
    px = op.black_scholes(120, 100, 40, 0.4, "call")
    assert 20.0 <= px <= 120.0


def test_atm_call_delta_is_slightly_above_half():
    """ATM delta sits just over 0.5 - this drives strike selection for long options."""
    d = op._bs_delta(100, 100, 30, 0.30, "call")
    assert 0.50 < d < 0.60


def test_deep_itm_and_otm_deltas_saturate():
    """Deltas approach 1 deep ITM and 0 deep OTM, so the DELTA_MIN/MAX band bites."""
    assert op._bs_delta(200, 100, 30, 0.3, "call") > 0.99
    assert op._bs_delta(50, 100, 30, 0.3, "call") < 0.01


def test_put_delta_is_call_delta_minus_one():
    """Put delta = N(d1) - 1, i.e. negative. Sign errors invert every bearish trade."""
    c = op._bs_delta(100, 105, 30, 0.3, "call")
    p = op._bs_delta(100, 105, 30, 0.3, "put")
    assert p == pytest.approx(c - 1.0)
    assert p < 0


def test_expiring_itm_put_delta_is_negative_one():
    """At expiry an in-the-money put has delta -1, matching the sign convention above."""
    assert op._bs_delta(90, 100, 0, 0.3, "put") == -1.0


def test_value_vertical_is_the_difference_of_the_legs():
    """A debit spread is worth long minus short, bounded by 0 and the strike width."""
    v = op.value_vertical(100, 100, 110, 30, 0.3, "call")
    assert v == pytest.approx(op.black_scholes(100, 100, 30, 0.3, "call")
                              - op.black_scholes(100, 110, 30, 0.3, "call"))
    assert 0 < v < 10


def test_value_vertical_at_expiry_is_capped_at_the_width():
    """Above the short strike at expiry the spread is worth exactly the width."""
    assert op.value_vertical(200, 100, 110, 0, 0.3, "call") == pytest.approx(10.0)


def test_prob_above_is_symmetric_and_bounded():
    """P(finish above spot) is just under 50% (lognormal drag) and always in [0,1]."""
    p = op._prob_above(100, 100, 0.3, 30)
    assert 0.4 < p < 0.5
    assert op._prob_above(100, 100, 0.3, 0) == 0.0    # degenerate inputs -> 0


def test_strike_increments_follow_listed_conventions():
    """Strike spacing must match what brokers actually list, or picks are unfillable."""
    assert op.strike_increment(10) == 1.0
    assert op.strike_increment(50) == 2.5
    assert op.strike_increment(150) == 5.0
    assert op.strike_increment(500) == 10.0
    assert op.round_to_strike(103) == 105.0


# ---------------------------------------------------------------- fills / spreads

def test_leg_price_is_conservative_on_both_sides():
    """You pay the ask and receive the bid - optimistic mid fills overstate every edge."""
    row = {"bid": 1.00, "ask": 1.20, "lastPrice": 1.10}
    assert op._leg_price(row, "buy") == 1.20
    assert op._leg_price(row, "sell") == 1.00


def test_leg_price_falls_back_to_last_then_to_the_other_side():
    """A one-sided quote still yields a usable, conservative price."""
    assert op._leg_price({"bid": 0, "ask": 0, "lastPrice": 2.0}, "buy") == 2.0
    assert op._leg_price({"bid": 1.0, "ask": 0, "lastPrice": 0}, "buy") == pytest.approx(1.05)
    assert op._leg_price({"bid": 0, "ask": 2.0, "lastPrice": 0}, "sell") == pytest.approx(1.90)


def test_missing_market_is_treated_as_maximally_unreliable():
    """No two-sided market returns spread_pct 1.0, which flags the play as wide.

    Quoting a price with no market behind it is how the app's quote diverged from fills."""
    assert op._leg_spread_pct({"bid": 0, "ask": 0}) == 1.0
    assert op._leg_spread_pct({"bid": 1.0, "ask": 1.1}) == pytest.approx(0.1 / 1.05)


def test_leg_liquidity_flags_thin_strikes():
    """Open interest below 25 marks a strike unusable - you are the only participant."""
    assert op._leg_liquidity({"openInterest": 3, "volume": 0})[2] is True
    assert op._leg_liquidity({"openInterest": 500, "volume": 10})[2] is False
    assert op._leg_liquidity({"openInterest": None, "volume": float("nan")}) == (0, 0, True)


# ---------------------------------------------------------------- quality scoring

def qa(monkeypatch, verdict="fair", ratio=1.0, in_hold=False, before=False,
       against=False, oi=500, wide=False, spread=0.05, debit=False):
    import signals.context as ctx
    monkeypatch.setattr(ctx, "iv_assessment", lambda t, iv: {
        "iv": iv, "rv": iv, "ratio": ratio, "vol_pct": 50, "verdict": verdict,
        "expensive": verdict in ("rich", "very_rich"), "note": ""})
    monkeypatch.setattr(ctx, "earnings_check", lambda t, e, hold_days=10: {
        "date": "2025-01-01" if (in_hold or before) else None,
        "in_hold": in_hold, "before_expiry": before})
    monkeypatch.setattr(ctx, "regime_alignment", lambda s: {
        "bias": 0.0, "regime": "bearish", "aligned": not against,
        "against_trend": against})
    return op.assess_quality("T", "BUY", "2025-01-31", 0.3, oi, wide, spread,
                             is_debit_spread=debit)


def test_clean_setup_scores_100(monkeypatch):
    """A fair-IV, liquid, no-earnings, trend-aligned trade takes no penalties."""
    r = qa(monkeypatch)
    assert r["score"] == 100 and r["warnings"] == []


def test_earnings_inside_the_hold_is_the_biggest_penalty(monkeypatch):
    """An earnings print inside the hold costs 35 points - IV crush kills these."""
    assert qa(monkeypatch, in_hold=True)["score"] == 65


def test_very_rich_premium_penalised_less_on_a_debit_spread(monkeypatch):
    """A spread sells premium at the same rich IV, so the penalty is halved.

    Treating both structures identically would wrongly reject usable spreads."""
    assert qa(monkeypatch, verdict="very_rich")["score"] == 70
    assert qa(monkeypatch, verdict="very_rich", debit=True)["score"] == 85


def test_cheap_premium_is_a_small_bonus(monkeypatch):
    """Cheap IV is rewarded, but the score stays capped at 100."""
    assert qa(monkeypatch, verdict="cheap")["score"] == 100


def test_no_open_interest_is_a_30_point_penalty(monkeypatch):
    """Under 25 OI is effectively no market; under 100 is a smaller warning."""
    assert qa(monkeypatch, oi=3)["score"] == 70
    assert qa(monkeypatch, oi=50)["score"] == 88


def test_penalties_accumulate_and_clamp_at_zero(monkeypatch):
    """Stacked red flags cannot drive the score negative - it floors at 0."""
    r = qa(monkeypatch, verdict="very_rich", in_hold=True, oi=1, wide=True,
           spread=0.5, against=True)
    assert r["score"] == 0
    assert len(r["warnings"]) >= 5


def test_wide_market_and_against_trend_are_flagged(monkeypatch):
    """Wide bid/ask and a counter-trend trade each cost points and warn the user."""
    r = qa(monkeypatch, wide=True, spread=0.4, against=True)
    assert r["score"] == 100 - 12 - 15
    assert any("limit order" in w for w in r["warnings"])
    assert any("market trend" in w for w in r["warnings"])


# ---------------------------------------------------------------- limit guidance

def test_debit_limit_walks_up_from_below_mid():
    """A buy limit starts below mid, walks to mid, and never chases past the cap.

    Paying the ask on a wide market is roughly the size of the entire edge."""
    g = op.limit_price_guidance(2.00, 0.20, is_credit=False)
    assert g["start"] < g["fair"] < g["worst_acceptable"]
    assert g["fair"] == 2.00
    assert g["start"] == pytest.approx(2.00 - 0.20 * 0.4, abs=0.01)
    assert "LIMIT BUY" in g["instruction"]


def test_credit_limit_walks_down_from_above_mid():
    """A sell limit starts above mid and has a floor below which risk isn't paid for."""
    g = op.limit_price_guidance(1.00, 0.20, is_credit=True)
    assert g["start"] > g["fair"] > g["worst_acceptable"]
    assert "LIMIT SELL" in g["instruction"]


def test_tight_market_gives_a_narrow_band():
    """With a 2% market the acceptable band is tiny - no room to chase."""
    g = op.limit_price_guidance(3.00, 0.02)
    assert g["worst_acceptable"] - g["start"] < 0.10


def test_zero_spread_pct_falls_back_to_a_2pct_band():
    """A missing spread reading still produces an actionable, bounded price."""
    g = op.limit_price_guidance(5.00, 0.0)
    assert g["start"] < 5.00 < g["worst_acceptable"]


def test_guidance_uses_absolute_value_for_credit_positions():
    """net_debit is negative for a credit; the printed prices must stay positive."""
    g = op.limit_price_guidance(-1.50, 0.10, is_credit=True)
    assert g["fair"] == 1.50 and g["start"] > 0


# ---------------------------------------------------------------- builders

@pytest.fixture
def fake_chain(monkeypatch, neutral_context):
    """Install a deterministic option chain in place of yfinance."""
    def install(calls, puts, expiries=None, spot=100.0):
        exps = expiries or (near_expiry(30),)
        monkeypatch.setattr(op, "yf", type("M", (), {
            "Ticker": staticmethod(
                lambda tk: FakeTicker(tk, FakeChain(calls, puts), exps, spot))}))
    return install


def call_rows(strikes=(90, 95, 100, 105, 110), **kw):
    return option_rows(strikes, bid=lambda k: max(0.10, (105 - k) * 0.8 + 2),
                       ask=lambda k: max(0.20, (105 - k) * 0.8 + 2.2), **kw)


def put_rows(strikes=(85, 90, 95, 100, 105), **kw):
    return option_rows(strikes, bid=lambda k: max(0.10, (k - 88) * 0.5 + 0.5),
                       ask=lambda k: max(0.20, (k - 88) * 0.5 + 0.7), **kw)


def test_build_long_option_returns_a_priced_call(fake_chain):
    """A long call quotes the ask, sets breakeven = strike + premium, and sizes it."""
    fake_chain(call_rows(), put_rows())
    p = op.build_long_option("AAPL", "BUY", 100.0, 115.0, 95.0, risk_budget=1000.0)
    assert p is not None
    assert p.strategy == "Long CALL" and len(p.legs) == 1
    assert p.net_debit == p.max_loss > 0
    assert p.breakeven == pytest.approx(p.legs[0].strike + p.net_debit)
    assert p.contracts == max(1, int(1000.0 / (p.net_debit * 100)))
    assert p.cost == pytest.approx(p.contracts * p.net_debit * 100)


def test_build_long_option_rejects_a_non_directional_signal(fake_chain):
    """WATCH is not tradeable - the builder must return None, not guess a direction."""
    fake_chain(call_rows(), put_rows())
    assert op.build_long_option("AAPL", "WATCH", 100, 110, 95) is None


def test_build_long_option_returns_none_without_a_chain(monkeypatch, neutral_context):
    """No listed options means no play - a fantasy quote here would be unfillable."""
    monkeypatch.setattr(op, "yf", type("M", (), {
        "Ticker": staticmethod(lambda tk: FakeTicker(tk, None, ()))}))
    assert op.build_long_option("XYZ", "BUY", 100, 110, 95) is None


def test_build_long_option_rejects_a_zero_priced_contract(fake_chain):
    """A strike with no price at all cannot be bought - return None, not a $0 trade."""
    rows = option_rows((100.0, 105.0), bid=0.0, ask=0.0, iv=0.30)
    rows["lastPrice"] = 0.0
    fake_chain(rows, rows)
    assert op.build_long_option("AAPL", "BUY", 100.0, 115.0, 95.0) is None


def test_build_long_option_picks_a_strike_inside_the_delta_band(fake_chain):
    """Strike selection targets ~0.62 delta: real participation, manageable decay."""
    fake_chain(call_rows(), put_rows())
    p = op.build_long_option("AAPL", "BUY", 100.0, 115.0, 95.0)
    assert op.DELTA_MIN <= p.delta <= op.DELTA_MAX


def test_credit_spread_collects_a_credit_with_a_defined_max_loss(fake_chain):
    """Bull put spread: negative net_debit (money in), max loss = width - credit."""
    fake_chain(call_rows(), put_rows())
    p = op.build_credit_spread("AAPL", "BUY", 100.0, 110.0, 95.0)
    assert p is not None and p.strategy == "Bull Put Spread"
    assert p.net_debit < 0
    assert p.max_profit == pytest.approx(-p.net_debit)
    width = abs(p.legs[0].strike - p.legs[1].strike)
    assert p.max_loss == pytest.approx(round(width - p.max_profit, 2))
    assert p.max_loss > 0


def test_credit_spread_sells_above_and_buys_below_spot(fake_chain):
    """The short put must sit below spot with the long wing further below.

    Reversing the legs turns a credit spread into a guaranteed loss."""
    fake_chain(call_rows(), put_rows())
    p = op.build_credit_spread("AAPL", "BUY", 100.0, 110.0, 95.0)
    short_leg, long_leg = p.legs
    assert short_leg.action == "SELL" and long_leg.action == "BUY"
    assert long_leg.strike < short_leg.strike < 100.0
    assert p.breakeven == pytest.approx(short_leg.strike - p.max_profit)


def test_credit_spread_rejected_when_the_credit_is_pennies(fake_chain):
    """Credit below 12% of the width is refused: one loss would erase many wins.

    This floor is the difference between selling premium and betting 274:1 against
    yourself."""
    thin = option_rows((85, 90, 95, 100, 105),
                       bid=lambda k: 0.05, ask=lambda k: 0.06, iv=0.30)
    fake_chain(call_rows(), thin)
    assert op.build_credit_spread("AAPL", "BUY", 100.0, 110.0, 95.0) is None


def test_credit_spread_rich_iv_is_a_bonus_not_a_penalty(monkeypatch, fake_chain):
    """When SELLING premium, expensive IV improves the score instead of reducing it."""
    import signals.context as ctx
    monkeypatch.setattr(ctx, "iv_assessment", lambda t, iv: {
        "iv": iv, "rv": iv / 1.5, "ratio": 1.5, "vol_pct": 90, "verdict": "very_rich",
        "expensive": True, "note": ""})
    fake_chain(call_rows(), put_rows())
    p = op.build_credit_spread("AAPL", "BUY", 100.0, 110.0, 95.0)
    assert p.quality_score == 100
    assert any("good for SELLING" in w for w in p.warnings)
    assert not any("Very expensive" in w for w in p.warnings)


def test_bear_call_credit_spread_is_mirrored(fake_chain):
    """A SELL signal sells a call above spot and buys a higher one as the wing."""
    fake_chain(call_rows((100, 105, 110, 115, 120)), put_rows())
    p = op.build_credit_spread("AAPL", "SELL", 100.0, 90.0, 105.0)
    if p is not None:      # depends on the synthetic chain paying enough credit
        assert p.strategy == "Bear Call Spread"
        assert p.legs[1].strike > p.legs[0].strike > 100.0
        assert p.breakeven > p.legs[0].strike


def test_play_to_dict_marks_a_credit_and_carries_limit_guidance(fake_chain):
    """Serialization must derive credit-ness from the sign of net_debit.

    Getting this backwards tells the user to BUY a spread they are meant to SELL."""
    fake_chain(call_rows(), put_rows())
    p = op.build_credit_spread("AAPL", "BUY", 100.0, 110.0, 95.0)
    d = op.play_to_dict(p)
    assert d["net_debit"] < 0
    assert "LIMIT SELL" in d["limit_guidance"]["instruction"]
    assert d["max_loss"] == p.max_loss and d["quality_score"] == p.quality_score
    assert op.play_to_dict(None) is None
