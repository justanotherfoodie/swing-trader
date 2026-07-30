"""Realized-P&L accounting: scale_out, close_position, performance_stats, _attribution.

These numbers ARE the user's track record and feed the risk circuit breaker. If
realized P&L is wrong, the drawdown maths, the consecutive-loss pause and every
attribution conclusion are wrong too.
"""

import json

import pytest

from conftest import make_position, write_positions
import portfolio as pf


def load(path):
    return json.loads(path.read_text())


# ---------------------------------------------------------------- scale_out

def test_scale_out_records_partial_and_keeps_position_open(portfolio_file):
    """Selling 2 of 4 leaves 2 open and books only the sold contracts' P&L.

    The whole profit ladder depends on a partially-closed position being a real state."""
    write_positions(portfolio_file, [make_position(contracts=4, net_debit=5.0)])
    out = pf.scale_out("abc123", 2, exit_value=1400.0)   # cost of 2 = $1000
    p = out[0]
    assert p["contracts"] == 2
    assert p["status"] == "open"
    assert p["realized_pnl"] == 400.0
    assert p["partial_exits"][0]["contracts"] == 2


def test_scale_out_cannot_sell_more_than_held(portfolio_file):
    """Over-selling is clamped to the position size, never negative contracts.

    A negative contract count would invert the sign of all later P&L."""
    write_positions(portfolio_file, [make_position(contracts=2)])
    out = pf.scale_out("abc123", 99, exit_value=0.0)
    assert out[0]["contracts"] == 0
    assert out[0]["status"] == "closed"


def test_scale_out_of_last_contract_closes_position(portfolio_file):
    """Scaling out the final contract must flip status to closed with a timestamp."""
    write_positions(portfolio_file, [make_position(contracts=1)])
    out = pf.scale_out("abc123", 1, exit_value=800.0)
    assert out[0]["status"] == "closed"
    assert "closed_at" in out[0]


def test_scale_out_accumulates_across_two_rungs(portfolio_file):
    """Two successive scale-outs sum their realized P&L rather than replacing it."""
    write_positions(portfolio_file, [make_position(contracts=4, net_debit=5.0)])
    pf.scale_out("abc123", 2, exit_value=1400.0)   # +400
    out = pf.scale_out("abc123", 1, exit_value=800.0)   # cost 500 -> +300
    assert out[0]["realized_pnl"] == 700.0
    assert out[0]["contracts"] == 1


# ---------------------------------------------------------------- close_position

def test_close_position_after_partial_accumulates_pnl(portfolio_file):
    """REGRESSION: closing after a scale-out must ADD to realized_pnl, not overwrite it.

    The historic bug overwrote it, silently erasing a banked partial profit from the
    track record the moment the remainder was closed - the equity curve, drawdown and
    circuit breaker all then ran on fiction."""
    write_positions(portfolio_file, [make_position(contracts=4, net_debit=5.0)])
    pf.scale_out("abc123", 2, exit_value=1400.0)          # banked +400
    out = pf.close_position("abc123", exit_value=1200.0)  # 2 left, cost 1000 -> +200
    assert out[0]["realized_pnl"] == 600.0, "partial profit was erased on final close"
    assert out[0]["status"] == "closed"


def test_close_position_cost_basis_is_remaining_contracts_only(portfolio_file):
    """The final leg's cost basis is the contracts still open, not the original size.

    Using the original count double-counts the scaled-out contracts and fabricates a loss."""
    write_positions(portfolio_file, [make_position(contracts=4, net_debit=5.0)])
    pf.scale_out("abc123", 2, exit_value=1000.0)          # flat
    out = pf.close_position("abc123", exit_value=1000.0)  # 2 @ $500 cost -> flat
    assert out[0]["realized_pnl"] == 0.0


def test_close_position_without_exit_value_records_no_pnl(portfolio_file):
    """Closing with no exit value marks it closed but leaves P&L untracked.

    Inventing a zero would pollute the win rate with fake breakeven trades."""
    write_positions(portfolio_file, [make_position()])
    out = pf.close_position("abc123")
    assert out[0]["status"] == "closed"
    assert "realized_pnl" not in out[0]


def test_close_unknown_id_is_a_no_op(portfolio_file):
    """An unrecognised id must not mutate or drop any position."""
    write_positions(portfolio_file, [make_position()])
    out = pf.close_position("nope", exit_value=100.0)
    assert out[0]["status"] == "open"


# ---------------------------------------------------------------- open_positions

def test_open_positions_persists_entry_context_for_attribution(portfolio_file):
    """Entry conditions are journalled at open time; without them attribution is impossible."""
    pf.open_positions([{
        "ticker": "MSFT", "kind": "call", "expiry": "2030-01-18",
        "long_strike": 400.0, "short_strike": None, "net_debit": 6.0, "contracts": 2,
        "entry_spot": 400.0, "target": 430.0, "stop": 390.0,
        "strategies": ["EMA 9/21 Crossover"], "iv_verdict": "fair", "quality_score": 80,
    }])
    p = load(portfolio_file)[0]
    assert p["status"] == "open"
    assert p["peak_pnl_pct"] == 0.0 and p["scaled_out"] == []
    assert p["entry_context"]["strategies"] == ["EMA 9/21 Crossover"]
    assert len(p["id"]) == 8


# ---------------------------------------------------------------- stats

def test_performance_stats_win_rate_and_averages(portfolio_file):
    """Win rate/averages count only closed trades that actually have a P&L figure.

    Counting untracked closes as zeros would understate both edge and losses."""
    write_positions(portfolio_file, [
        make_position(id="a", status="closed", realized_pnl=300.0),
        make_position(id="b", status="closed", realized_pnl=-100.0),
        make_position(id="c", status="closed"),          # untracked
        make_position(id="d", status="open"),
    ])
    s = pf.performance_stats()
    assert s["closed_total"] == 3
    assert s["tracked_pnl_count"] == 2 and s["untracked_count"] == 1
    assert s["open_count"] == 1
    assert s["realized_pnl"] == 200.0
    assert s["wins"] == 1 and s["losses"] == 1 and s["win_rate"] == 50
    assert s["avg_win"] == 300.0 and s["avg_loss"] == -100.0


def test_performance_stats_empty_portfolio(portfolio_file):
    """No trades yet must give win_rate None (unknown), not 0% (a false verdict)."""
    write_positions(portfolio_file, [])
    s = pf.performance_stats()
    assert s["win_rate"] is None
    assert s["realized_pnl"] == 0 and s["avg_win"] == 0


def test_breakeven_trade_counts_as_a_loss(portfolio_file):
    """Exactly zero P&L is classified as a loss - commissions make flat a loser."""
    write_positions(portfolio_file, [make_position(status="closed", realized_pnl=0.0)])
    s = pf.performance_stats()
    assert s["losses"] == 1 and s["wins"] == 0


def test_attribution_groups_by_each_strategy_in_the_list(portfolio_file):
    """A trade credited to two strategies appears under both buckets.

    This is the feedback loop that decides which strategies get disabled."""
    trades = [
        {"realized_pnl": 200.0,
         "entry_context": {"strategies": ["A", "B"], "iv_verdict": "rich"}},
        {"realized_pnl": -50.0,
         "entry_context": {"strategies": ["B"], "iv_verdict": "rich"}},
    ]
    out = {d["key"]: d for d in pf._attribution(trades, "strategies")}
    assert out["A"]["trades"] == 1 and out["A"]["win_rate"] == 100
    assert out["B"]["trades"] == 2 and out["B"]["total_pnl"] == 150.0
    assert out["B"]["win_rate"] == 50
    assert out["B"]["avg_pnl"] == 75.0


def test_attribution_ignores_missing_context(portfolio_file):
    """Trades with no entry_context are skipped, not bucketed under None.

    A phantom 'None' strategy row would look like a real, losing strategy."""
    out = pf._attribution([{"realized_pnl": 10.0},
                           {"realized_pnl": 10.0, "entry_context": {}}], "strategies")
    assert out == []


def test_attribution_sorted_by_total_pnl(portfolio_file):
    """Best-performing bucket first, so the ranking the user acts on is correct."""
    trades = [{"realized_pnl": 10.0, "entry_context": {"iv_verdict": "cheap"}},
              {"realized_pnl": 90.0, "entry_context": {"iv_verdict": "fair"}}]
    out = pf._attribution(trades, "iv_verdict")
    assert [d["key"] for d in out] == ["fair", "cheap"]


# --------------------------------------------- ladder bookkeeping (regression tests)

def test_scale_out_records_the_tier_that_fired_and_everything_below(portfolio_file):
    """A tier-2 scale must close out tier 1 as well, or the runner gets sold twice.

    scale_out used to label the rung by COUNT: the first scale became "t1" whatever
    actually triggered it. So a position that gapped straight to +50% and took tier 2
    was recorded as having taken tier 1. On the next evaluate the t2 branch was
    correctly skipped, but the t1 branch saw "t1" missing from `scaled_out`, fired, and
    sold half the remaining contracts a second time at a rung already taken.

    Marking every rung at or below the one that fired is what closes that hole - you
    cannot arrive at tier 2 without having passed through tier 1's threshold.
    """
    write_positions(portfolio_file, [make_position(contracts=4, scaled_out=[])])
    out = pf.scale_out("abc123", 2, exit_value=1400.0, tier="t2")
    assert out[0]["scaled_out"] == ["t1", "t2"]


def test_scale_out_tier1_does_not_preemptively_consume_tier2(portfolio_file):
    """Taking tier 1 must leave tier 2 available for the remaining contracts."""
    write_positions(portfolio_file, [make_position(contracts=4, scaled_out=[])])
    out = pf.scale_out("abc123", 2, exit_value=700.0, tier="t1")
    assert out[0]["scaled_out"] == ["t1"]


def test_update_position_preserves_peak_when_not_supplied(portfolio_file):
    """Correcting the contract count must not wipe the profit-lock high-water mark.

    update_position used to write `peak_pnl_pct` unconditionally, defaulting to 0.0.
    Fixing an unrelated field on a position already up 60% therefore erased the peak
    and disarmed the trailing exit, so a winner could round-trip to breakeven with no
    SELL ever firing.
    """
    write_positions(portfolio_file, [make_position(peak_pnl_pct=55.0)])
    out = pf.update_position("abc123", {"contracts": 3})
    assert out[0]["peak_pnl_pct"] == 55.0


def test_update_position_still_sets_peak_when_explicitly_supplied(portfolio_file):
    """An explicit peak_pnl_pct must still be honoured (e.g. resetting after a fix-up)."""
    write_positions(portfolio_file, [make_position(peak_pnl_pct=55.0)])
    out = pf.update_position("abc123", {"peak_pnl_pct": 0.0})
    assert out[0]["peak_pnl_pct"] == 0.0


def test_update_position_only_writes_allowed_fields(portfolio_file):
    """Arbitrary keys (e.g. realized_pnl, status) cannot be injected via update.

    An unfiltered update would let a UI call rewrite the realized track record."""
    write_positions(portfolio_file, [make_position()])
    out = pf.update_position("abc123", {"contracts": 2, "realized_pnl": 99999,
                                        "status": "closed"})
    assert out[0]["contracts"] == 2
    assert out[0]["status"] == "open"
    assert "realized_pnl" not in out[0]


def test_update_position_nulling_short_strike_relabels_as_single(portfolio_file):
    """Setting short_strike to null converts a mis-recorded spread into a long option.

    Downstream P&L uses short_strike to pick the valuation formula."""
    write_positions(portfolio_file, [make_position(short_strike=110.0,
                                                   strategy="Bull Call Spread")])
    out = pf.update_position("abc123", {"short_strike": None})
    assert out[0]["short_strike"] is None
    assert out[0]["strategy"] == "Long CALL"


def test_load_survives_corrupt_portfolio_file(portfolio_file):
    """A truncated/corrupt JSON file returns [] instead of crashing the API.

    A hard failure here takes down every endpoint, including the exit verdicts."""
    portfolio_file.write_text("{not json")
    assert pf._load() == []
