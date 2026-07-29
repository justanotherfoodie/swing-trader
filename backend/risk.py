"""
Account-level risk control: the layer that keeps a bad run from becoming a blown account.

The planner already sizes individual trades. This governs the things no single trade can
see:

  1. RISK AS A SHARE OF EQUITY, not a fixed dollar amount. A flat "$200 per trade" risks
     33% of a $600 account and 2% of a $10,000 one. Percentage-based sizing shrinks
     automatically when you are losing and grows when you are winning, which is the
     single most important survival mechanic in trading.

  2. PORTFOLIO HEAT. Four positions each risking 5% is not four independent bets - in a
     market-wide selloff, equities correlate and it behaves like one bet risking 20%.
     Total open risk is capped, and options get an extra haircut because a long option
     can genuinely go to zero.

  3. DRAWDOWN CIRCUIT BREAKER. Fixed, mechanical rules that cut size or stop trading
     after losses. Traders who blow up rarely do it on one trade; they do it by holding
     size constant, or increasing it, through a losing streak.

State is derived from portfolio.json rather than stored separately, so it can never
disagree with the actual trade history.
"""

from dataclasses import dataclass
from datetime import datetime

# ---- sizing -------------------------------------------------------------------
# Risk per trade as a share of current equity. 1-2% is the conventional band; long
# options sit at the low end because "risk" for them means the entire premium.
BASE_RISK_PCT = 0.02
MIN_RISK_PCT = 0.005

# Total risk allowed across all open positions at once.
MAX_PORTFOLIO_HEAT = 0.06        # 6% of equity
MAX_OPEN_POSITIONS = 4

# ---- circuit breaker ----------------------------------------------------------
DD_HALVE_SIZE = 0.10             # 10% off peak equity -> half size
DD_STOP_TRADING = 0.20           # 20% off peak -> stop, review before resuming
CONSECUTIVE_LOSS_PAUSE = 4       # this many losses in a row -> mandatory pause

# The cheapest realistically liquid option contract. Below roughly this, you are into
# far-OTM lottery tickets and pennies-wide strikes nobody trades.
TYPICAL_MIN_CONTRACT_COST = 100.0


@dataclass
class RiskState:
    equity: float
    peak_equity: float
    drawdown_pct: float
    open_positions: int
    open_risk: float             # dollars genuinely at risk right now
    heat_pct: float              # open_risk as a share of equity
    consecutive_losses: int
    size_multiplier: float       # 1.0 normal, 0.5 throttled, 0.0 halted
    can_trade: bool
    risk_per_trade: float        # dollars to risk on the next trade
    status: str                  # normal | throttled | halted
    messages: list
    undersized: bool = False     # proper risk sizing can't buy a contract
    implied_risk_pct: float = 0.0  # what one contract actually risks, as % of equity


def _closed_in_order(positions: list[dict]) -> list[dict]:
    closed = [p for p in positions
              if p.get("status") == "closed" and p.get("realized_pnl") is not None]
    return sorted(closed, key=lambda p: p.get("closed_at") or "")


def consecutive_losses(positions: list[dict]) -> int:
    n = 0
    for p in reversed(_closed_in_order(positions)):
        if p["realized_pnl"] <= 0:
            n += 1
        else:
            break
    return n


def equity_curve(positions: list[dict], starting_equity: float) -> tuple[float, float]:
    """(current_equity, peak_equity) from realized P&L in close order."""
    eq = starting_equity
    peak = starting_equity
    for p in _closed_in_order(positions):
        eq += p["realized_pnl"]
        peak = max(peak, eq)
    return eq, peak


def open_risk_dollars(positions: list[dict]) -> float:
    """Money genuinely at risk in open positions.

    For a long option or debit spread the answer is simple and brutal: the entire premium
    paid. There is no stop that reliably saves you from a gap, so we count the whole thing.
    """
    total = 0.0
    for p in positions:
        if p.get("status") != "open":
            continue
        total += float(p.get("net_debit", 0)) * 100 * int(p.get("contracts", 0))
    return round(total, 2)


def assess(positions: list[dict], starting_equity: float = 600.0,
           cash_available: float | None = None) -> RiskState:
    equity, peak = equity_curve(positions, starting_equity)
    dd = (peak - equity) / peak if peak > 0 else 0.0
    losses = consecutive_losses(positions)
    open_pos = [p for p in positions if p.get("status") == "open"]
    risk_now = open_risk_dollars(positions)
    heat = risk_now / equity if equity > 0 else 0.0

    messages: list[str] = []
    mult = 1.0
    status = "normal"

    # --- circuit breaker, worst condition wins ---
    if dd >= DD_STOP_TRADING:
        mult, status = 0.0, "halted"
        messages.append(
            f"Account is {dd*100:.0f}% below its peak. Trading halted. Review what changed "
            "before putting more capital at risk - this is the rule that prevents a bad "
            "stretch from becoming a permanent one.")
    elif dd >= DD_HALVE_SIZE:
        mult, status = 0.5, "throttled"
        messages.append(
            f"Account is {dd*100:.0f}% below its peak. Position size halved until it "
            "recovers. Smaller size during a drawdown is how you stay solvent long enough "
            "to benefit when conditions turn.")

    if losses >= CONSECUTIVE_LOSS_PAUSE and status != "halted":
        mult, status = 0.0, "halted"
        messages.append(
            f"{losses} losing trades in a row. Mandatory pause. Either the market changed "
            "or the process slipped; find out which before the next trade.")

    # --- heat ---
    if heat >= MAX_PORTFOLIO_HEAT:
        messages.append(
            f"Portfolio heat {heat*100:.1f}% of equity is at/above the {MAX_PORTFOLIO_HEAT*100:.0f}% "
            "cap. No new positions until something closes - correlated positions behave "
            "like one big bet when the market moves against you.")
        mult = 0.0
    elif heat >= MAX_PORTFOLIO_HEAT * 0.75:
        messages.append(f"Portfolio heat {heat*100:.1f}% - approaching the "
                        f"{MAX_PORTFOLIO_HEAT*100:.0f}% cap. Size the next one down.")

    if len(open_pos) >= MAX_OPEN_POSITIONS:
        messages.append(f"{len(open_pos)} positions already open (max {MAX_OPEN_POSITIONS}). "
                        "More positions than you can genuinely monitor is its own risk.")
        mult = 0.0

    risk_pct = max(MIN_RISK_PCT, BASE_RISK_PCT * mult)
    risk_per_trade = round(equity * risk_pct, 2) if mult > 0 else 0.0

    # Never let a single suggested trade exceed the cash actually on hand.
    if cash_available is not None:
        risk_per_trade = min(risk_per_trade, cash_available)

    # Headroom against the heat cap also constrains the next trade.
    headroom = max(0.0, equity * MAX_PORTFOLIO_HEAT - risk_now)
    risk_per_trade = round(min(risk_per_trade, headroom), 2)

    # --- the uncomfortable arithmetic of a small options account ---------------
    # A textbook 2% risk budget on a small balance cannot buy a single contract. Rather
    # than quietly emitting an unusable number, or silently abandoning the rule, state
    # the conflict and quantify the real risk being taken.
    undersized = False
    implied_pct = 0.0
    if mult > 0 and risk_per_trade < TYPICAL_MIN_CONTRACT_COST:
        undersized = True
        implied_pct = TYPICAL_MIN_CONTRACT_COST / equity * 100 if equity else 0
        equity_needed = TYPICAL_MIN_CONTRACT_COST / BASE_RISK_PCT
        messages.append(
            f"ACCOUNT UNDERSIZED FOR THIS STRATEGY. Proper risk sizing gives "
            f"${risk_per_trade:,.0f} per trade, but the cheapest realistic contract costs "
            f"around ${TYPICAL_MIN_CONTRACT_COST:,.0f}. Buying even one contract risks about "
            f"{implied_pct:.0f}% of the account - not the {BASE_RISK_PCT*100:.0f}% a "
            f"professional would accept. Trading options at textbook risk needs roughly "
            f"${equity_needed:,.0f}. Until then you are choosing between undersized "
            f"discipline and oversized bets; know which one you are picking.")

    if not messages:
        messages.append(
            f"Normal risk. Equity ${equity:,.0f}, {heat*100:.1f}% heat, "
            f"risking ${risk_per_trade:,.0f} ({risk_pct*100:.1f}%) on the next trade.")

    return RiskState(
        equity=round(equity, 2), peak_equity=round(peak, 2),
        drawdown_pct=round(dd * 100, 1),
        open_positions=len(open_pos), open_risk=risk_now,
        heat_pct=round(heat * 100, 1), consecutive_losses=losses,
        size_multiplier=mult, can_trade=mult > 0 and risk_per_trade > 0,
        risk_per_trade=risk_per_trade, status=status, messages=messages,
        undersized=undersized, implied_risk_pct=round(implied_pct, 1),
    )


def to_dict(s: RiskState) -> dict:
    return {
        "equity": s.equity, "peak_equity": s.peak_equity,
        "drawdown_pct": s.drawdown_pct, "open_positions": s.open_positions,
        "open_risk": s.open_risk, "heat_pct": s.heat_pct,
        "max_heat_pct": MAX_PORTFOLIO_HEAT * 100,
        "consecutive_losses": s.consecutive_losses,
        "size_multiplier": s.size_multiplier, "can_trade": s.can_trade,
        "risk_per_trade": s.risk_per_trade, "status": s.status,
        "messages": s.messages,
        "undersized": s.undersized, "implied_risk_pct": s.implied_risk_pct,
    }
