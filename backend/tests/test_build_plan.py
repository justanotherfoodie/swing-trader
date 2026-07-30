"""portfolio.build_plan - budget allocation across the scan's best signals.

This decides how much of the user's money goes into which contract. The
concentration cap, the sector cap and the quality floor are the three rules that
stop one bad print from taking the account.
"""

import pytest

import portfolio as pf
from signals.options import OptionsPlay, OptionLeg


def play(net_debit=2.0, quality=100, strategy="Long CALL", kind="CALL", strike=100.0):
    """A minimal, fully-formed OptionsPlay standing in for a live quote."""
    return OptionsPlay(
        strategy=strategy, expiry="2030-01-18", dte=30,
        legs=[OptionLeg("BUY", kind, strike, net_debit)],
        net_debit=net_debit, max_profit=net_debit * 2, max_loss=net_debit,
        breakeven=strike + net_debit, risk_reward=2.0, prob_profit=50,
        contracts=1, cost=net_debit * 100, capital_vs_stock="", note="",
        max_spread_pct=0.05, wide_market=False, open_interest=500,
        delta=0.6, iv_verdict="fair", iv_ratio=1.0, warnings=[],
        quality_score=quality,
    )


def signal(ticker="AAA", direction="BUY", quality="high", confidence=80):
    return {
        "ticker": ticker, "signal": direction, "quality": quality,
        "confidence": confidence, "entry": 100.0, "take_profit_1": 110.0,
        "stop_loss": 95.0, "options_play": {"strategy": "scan-time quote"},
        "strategy_breakdown":
            [{"name": "EMA 9/21 Crossover", "score": 2.0}],
        "triggers": ["EMA 9/21 Crossover"], "source": "swing",
    }


@pytest.fixture
def plan_env(monkeypatch, portfolio_file, neutral_context):
    """Stub every live lookup build_plan makes, and control the per-ticker quote."""
    import signals.context as ctx
    quotes = {}
    sectors = {}

    monkeypatch.setattr(ctx, "get_sector", lambda t: sectors.get(t, "Unknown"))
    monkeypatch.setattr(ctx, "premium_buying_environment", lambda: {"verdict": "ok"})
    monkeypatch.setattr(ctx, "recommended_structure", lambda: {"pick": "single"})

    def builder(ticker, sig, entry, target, stop):
        return quotes.get(ticker)

    for name in ("build_long_option", "build_options_play", "build_credit_spread"):
        monkeypatch.setattr(pf, name, builder)
    portfolio_file.write_text("[]")
    return {"quotes": quotes, "sectors": sectors}


def test_empty_signal_list_explains_itself(plan_env):
    """No eligible signals returns an explained empty plan, not a blank screen."""
    p = pf.build_plan(1000.0, [])
    assert p["items"] == [] and p["cash_left"] == 1000.0
    assert "No options-eligible" in p["note"]


def test_unquotable_signal_is_dropped(plan_env):
    """A signal whose live re-quote fails is excluded rather than priced from stale data.

    Reusing the scan-time premium is how an evening plan quotes hours-old prices."""
    p = pf.build_plan(1000.0, [signal("AAA")])       # no quote registered
    assert p["items"] == []


def test_single_position_is_bought_and_costed(plan_env):
    """A funded signal produces contracts, a cost, and matching cash_left."""
    plan_env["quotes"]["AAA"] = play(net_debit=2.0)
    p = pf.build_plan(1000.0, [signal("AAA")])
    it = p["items"][0]
    assert it["per_contract"] == 200.0
    assert it["cost"] == it["per_contract"] * it["contracts"]
    assert p["total_cost"] + p["cash_left"] == pytest.approx(1000.0)


def test_no_single_ticker_exceeds_40pct_of_budget(plan_env):
    """The concentration cap holds even when only one cheap contract qualifies.

    Without it the cheapest contract absorbs the whole budget - one undiversified bet."""
    plan_env["quotes"]["AAA"] = play(net_debit=1.0)   # $100/contract
    p = pf.build_plan(1000.0, [signal("AAA")])
    assert p["items"][0]["cost"] <= 1000.0 * pf.MAX_POSITION_PCT


def test_total_cost_never_exceeds_the_budget(plan_env):
    """Allocation is hard-bounded by the budget - overspending is not recoverable."""
    for t in "ABCDEF":
        plan_env["quotes"][t] = play(net_debit=1.5)
    sigs = [signal(t, "BUY" if i % 2 else "SELL") for i, t in enumerate("ABCDEF")]
    p = pf.build_plan(1000.0, sigs)
    assert p["total_cost"] <= 1000.0
    assert p["cash_left"] >= 0


def test_one_position_per_sector(plan_env):
    """Two names in the same sector is one bet wearing two hats - only one is taken."""
    for t in ("AAA", "BBB"):
        plan_env["quotes"][t] = play(net_debit=1.0)
        plan_env["sectors"][t] = "Energy"
    p = pf.build_plan(1000.0, [signal("AAA"), signal("BBB")])
    assert len(p["items"]) == 1


def test_unknown_sector_does_not_block_diversification(plan_env):
    """'Unknown' sector is not treated as a bucket - it would collapse the whole plan."""
    for t in ("AAA", "BBB"):
        plan_env["quotes"][t] = play(net_debit=1.0)
    p = pf.build_plan(1000.0, [signal("AAA"), signal("BBB")], picks_per_side=2)
    assert len(p["items"]) == 2


def test_low_quality_plays_are_rejected_and_reported(plan_env):
    """Sub-50 quality plays are excluded outright but surfaced in `rejected`.

    These are the expensive-premium/earnings/no-liquidity setups that bleed money
    even when the direction is right - and a silent drop looks like a broken scanner."""
    plan_env["quotes"]["AAA"] = play(quality=30)
    p = pf.build_plan(1000.0, [signal("AAA")])
    assert p["items"] == []
    assert p["rejected"][0]["ticker"] == "AAA"
    assert p["rejected"][0]["quality_score"] == 30


def test_higher_quality_is_allocated_first(plan_env):
    """When budget is scarce, the cleaner setup wins the allocation."""
    plan_env["quotes"]["AAA"] = play(net_debit=3.0, quality=60)
    plan_env["quotes"]["BBB"] = play(net_debit=3.0, quality=95)
    p = pf.build_plan(400.0, [signal("AAA"), signal("BBB")], picks_per_side=1)
    assert [i["ticker"] for i in p["items"]] == ["BBB"]


def test_unaffordable_plan_explains_the_budget_maths(plan_env):
    """When nothing fits, the note names the cheapest contract and the 40% cap.

    An unexplained blank list is indistinguishable from a bug."""
    plan_env["quotes"]["AAA"] = play(net_debit=20.0)   # $2,000/contract
    p = pf.build_plan(1000.0, [signal("AAA")])
    assert p["items"] == []
    assert "nothing fits this budget" in p["note"]
    assert "2,000" in p["note"]


def test_credit_structure_commits_collateral_not_debit(plan_env):
    """For a credit spread the capital committed is max LOSS, not the (negative) debit.

    Sizing off net_debit would let the plan 'spend' negative money and oversell."""
    cs = play(net_debit=-1.0)
    cs.max_loss = 4.0
    plan_env["quotes"]["AAA"] = cs
    p = pf.build_plan(2000.0, [signal("AAA")], structure="credit")
    assert p["items"][0]["per_contract"] == 400.0
    assert "collateral" in p["note"]


def test_plan_carries_the_risk_state(plan_env):
    """The plan embeds the account risk verdict so a halted account is visible here."""
    plan_env["quotes"]["AAA"] = play()
    p = pf.build_plan(1000.0, [signal("AAA")])
    assert "risk" in p and "can_trade" in p["risk"]
    assert p["priced_at"].endswith("+00:00") or "T" in p["priced_at"]


def test_wide_market_names_are_called_out(plan_env):
    """A wide bid/ask ticker gets an explicit limit-order warning in the note."""
    q = play()
    q.wide_market = True
    plan_env["quotes"]["AAA"] = q
    p = pf.build_plan(1000.0, [signal("AAA")])
    assert "wide bid/ask" in p["note"]


def test_calls_and_puts_are_counted_separately(plan_env):
    """The call/put split drives the hedge story shown to the user."""
    plan_env["quotes"]["AAA"] = play(net_debit=2.0)
    plan_env["quotes"]["BBB"] = play(net_debit=2.0)
    p = pf.build_plan(1000.0, [signal("AAA", "BUY"), signal("BBB", "SELL")])
    assert p["n_call_contracts"] >= 1 and p["n_put_contracts"] >= 1
    kinds = {i["ticker"]: i["kind"] for i in p["items"]}
    assert kinds["AAA"] == "call" and kinds["BBB"] == "put"


def test_strategy_attribution_is_carried_into_the_plan(plan_env):
    """The firing strategy names travel with the item so closed trades can be attributed."""
    plan_env["quotes"]["AAA"] = play()
    p = pf.build_plan(1000.0, [signal("AAA")])
    assert p["items"][0]["strategies"] == ["EMA 9/21 Crossover"]
