"""Account-level risk control: circuit breaker, drawdown, heat, undersizing.

This module is the last line of defence against a losing streak becoming a blown
account. Every threshold is asserted at and around its boundary.
"""

import pytest

import risk


def closed(pnl, at):
    return {"status": "closed", "realized_pnl": pnl, "closed_at": at}


def open_pos(net_debit=1.0, contracts=1):
    return {"status": "open", "net_debit": net_debit, "contracts": contracts}


# ---------------------------------------------------------------- equity curve

def test_equity_curve_applies_pnl_in_close_order():
    """Equity and peak are replayed in close order so the peak is historically real.

    Summing out of order can hide a drawdown that actually happened."""
    pos = [closed(200.0, "2024-01-02"), closed(-150.0, "2024-01-03"),
           closed(50.0, "2024-01-01")]
    eq, peak = risk.equity_curve(pos, 1000.0)
    assert eq == 1100.0
    assert peak == 1250.0   # 1000 -> 1050 -> 1250 -> 1100


def test_equity_curve_ignores_open_and_untracked_positions():
    """Unrealized and untracked trades must not move the equity curve.

    Marking open positions into equity would let a paper gain unlock more size."""
    pos = [open_pos(), {"status": "closed", "closed_at": "2024-01-01"}]
    assert risk.equity_curve(pos, 600.0) == (600.0, 600.0)


def test_consecutive_losses_counts_back_from_most_recent():
    """The streak counter stops at the first win looking backwards."""
    pos = [closed(-10, "2024-01-01"), closed(20, "2024-01-02"),
           closed(-10, "2024-01-03"), closed(-10, "2024-01-04")]
    assert risk.consecutive_losses(pos) == 2


def test_breakeven_close_counts_toward_the_loss_streak():
    """Zero P&L counts as a loss for the pause rule - flat trades still cost fees."""
    assert risk.consecutive_losses([closed(0.0, "2024-01-01")]) == 1


def test_open_risk_is_the_entire_premium():
    """For long premium, risk = full debit x 100 x contracts; there is no safe stop.

    Counting only the distance to a stop understates gap risk to zero."""
    assert risk.open_risk_dollars([open_pos(2.5, 3), {"status": "closed"}]) == 750.0


# ---------------------------------------------------------------- circuit breaker

def test_drawdown_10pct_halves_size():
    """At exactly the 10% drawdown threshold size is halved, not stopped."""
    s = risk.assess([closed(-100.0, "2024-01-01")], starting_equity=1000.0)
    assert s.drawdown_pct == 10.0
    assert s.size_multiplier == 0.5 and s.status == "throttled"
    assert s.can_trade is True


def test_drawdown_just_below_threshold_stays_normal():
    """A 9.9% drawdown is still normal - the boundary must not fire early."""
    s = risk.assess([closed(-99.0, "2024-01-01")], starting_equity=1000.0)
    assert s.status == "normal" and s.size_multiplier == 1.0


def test_drawdown_20pct_halts_trading():
    """A 20% drawdown halts trading outright: review before risking more capital."""
    s = risk.assess([closed(-200.0, "2024-01-01")], starting_equity=1000.0)
    assert s.status == "halted"
    assert s.size_multiplier == 0.0 and s.can_trade is False
    assert s.risk_per_trade == 0.0


def test_four_consecutive_losses_forces_a_pause():
    """Four losses in a row halts even with a shallow drawdown."""
    pos = [closed(-10.0, f"2024-01-0{i}") for i in range(1, 5)]
    s = risk.assess(pos, starting_equity=10_000.0)
    assert s.consecutive_losses == 4
    assert s.status == "halted" and s.can_trade is False


def test_three_losses_do_not_pause():
    """Three losses is inside the tolerance - the pause must not trigger early."""
    pos = [closed(-10.0, f"2024-01-0{i}") for i in range(1, 4)]
    assert risk.assess(pos, starting_equity=10_000.0).status == "normal"


def test_drawdown_halt_is_not_downgraded_by_the_loss_rule():
    """A halted account stays halted; the worst condition must win."""
    pos = [closed(-300.0, "2024-01-01"), closed(-10.0, "2024-01-02")]
    s = risk.assess(pos, starting_equity=1000.0)
    assert s.status == "halted" and s.size_multiplier == 0.0


# ---------------------------------------------------------------- heat

def test_heat_at_cap_blocks_new_trades():
    """Open risk at/above 6% of equity stops new positions entirely.

    Correlated positions behave like one big bet in a selloff."""
    s = risk.assess([open_pos(6.0, 1)], starting_equity=10_000.0)   # $600 = 6%
    assert s.heat_pct == 6.0
    assert s.can_trade is False
    assert any("cap" in m for m in s.messages)


def test_heat_approaching_cap_warns_but_allows():
    """At 75% of the cap the user is warned and can still trade, sized down."""
    s = risk.assess([open_pos(5.0, 1)], starting_equity=10_000.0)   # 5%
    assert s.can_trade is True
    assert any("approaching" in m for m in s.messages)


def test_risk_per_trade_is_capped_by_remaining_heat_headroom():
    """The next trade can never exceed the unused portion of the heat budget.

    Without this the 6% cap could be breached by a single new position."""
    s = risk.assess([open_pos(5.5, 1)], starting_equity=10_000.0)   # $550 used of $600
    assert s.risk_per_trade == 50.0


def test_max_open_positions_blocks_new_trades():
    """Four open positions is the monitoring limit; a fifth is its own risk."""
    s = risk.assess([open_pos(0.5, 1) for _ in range(4)], starting_equity=100_000.0)
    assert s.open_positions == 4 and s.can_trade is False


def test_cash_available_caps_the_suggestion():
    """The suggested risk can never exceed the cash actually on hand."""
    s = risk.assess([], starting_equity=10_000.0, cash_available=75.0)
    assert s.risk_per_trade == 75.0


# ---------------------------------------------------------------- undersized

def test_small_account_is_flagged_undersized():
    """A $600 account risking 2% ($12) cannot buy a ~$100 contract - say so.

    Silently emitting an unusable size, or ignoring the rule, both mislead the user
    about how much of the account one contract really risks."""
    s = risk.assess([], starting_equity=600.0)
    assert s.undersized is True
    assert s.implied_risk_pct == pytest.approx(16.7, abs=0.1)
    assert any("UNDERSIZED" in m for m in s.messages)


def test_large_account_is_not_undersized():
    """At $10k the 2% budget ($200) buys a contract - no warning should appear."""
    s = risk.assess([], starting_equity=10_000.0)
    assert s.undersized is False
    assert s.risk_per_trade == 200.0
    assert not any("UNDERSIZED" in m for m in s.messages)


def test_undersized_boundary_at_exactly_100_dollars():
    """$5,000 equity gives exactly $100 - at the contract cost, so NOT undersized."""
    s = risk.assess([], starting_equity=5_000.0)
    assert s.risk_per_trade == 100.0
    assert s.undersized is False


def test_halted_account_is_not_labelled_undersized():
    """When trading is halted the undersizing message is noise - suppress it."""
    s = risk.assess([closed(-200.0, "2024-01-01")], starting_equity=1000.0)
    assert s.status == "halted" and s.undersized is False


def test_zero_equity_does_not_divide_by_zero():
    """A wiped-out account must return a halted state, not raise."""
    s = risk.assess([closed(-1000.0, "2024-01-01")], starting_equity=1000.0)
    assert s.equity == 0.0
    assert s.can_trade is False


def test_to_dict_exposes_the_fields_the_ui_relies_on():
    """The serialized state carries the cap so the UI can render heat vs limit."""
    d = risk.to_dict(risk.assess([], starting_equity=10_000.0))
    assert d["max_heat_pct"] == 6.0
    assert set(["equity", "drawdown_pct", "can_trade", "status",
                "undersized"]).issubset(d)
