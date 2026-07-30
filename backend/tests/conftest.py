"""Shared fixtures. Everything here is offline: no test may touch the network or the
real portfolio.json, because a test that hits yfinance is slow, flaky, and a test that
writes portfolio.json would corrupt a real money-tracking file."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

# backend/ must be importable as the package root ("portfolio", "risk", "signals.*")
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ---------------------------------------------------------------- synthetic data

def make_ohlcv(closes, volumes=None, highs=None, lows=None, opens=None):
    """Build a minimal OHLCV frame with the column names the app uses (lowercase)."""
    n = len(closes)
    closes = [float(c) for c in closes]
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": opens if opens is not None else closes,
        "high": highs if highs is not None else [c * 1.01 for c in closes],
        "low": lows if lows is not None else [c * 0.99 for c in closes],
        "close": closes,
        "volume": volumes if volumes is not None else [1_000_000] * n,
    }, index=idx)


@pytest.fixture
def uptrend_df():
    """80 bars of steady, low-noise uptrend - the 'clean bullish tape' baseline."""
    closes = [100 * (1.004 ** i) for i in range(80)]
    return make_ohlcv(closes)


@pytest.fixture
def flat_df():
    """80 bars of near-flat price - used to assert nothing fires without an event."""
    closes = [100 + (0.1 if i % 2 else -0.1) for i in range(80)]
    return make_ohlcv(closes)


# ---------------------------------------------------------------- portfolio file

@pytest.fixture
def portfolio_file(tmp_path, monkeypatch):
    """Redirect portfolio persistence to a temp file.

    Protects the user's real portfolio.json: any test that opened/closed positions
    against the live file would rewrite their actual trade record.
    """
    import portfolio as pf
    path = tmp_path / "portfolio.json"
    monkeypatch.setattr(pf, "_PORTFOLIO_FILE", str(path))
    return path


def write_positions(path, positions):
    path.write_text(json.dumps(positions))


def make_position(**over):
    """A realistic open single-leg long call position."""
    now = datetime.now(timezone.utc)
    p = {
        "id": "abc123",
        "ticker": "AAPL",
        "kind": "call",
        "strategy": "Long CALL",
        "expiry": (now + timedelta(days=30)).date().isoformat(),
        "long_strike": 100.0,
        "short_strike": None,
        "net_debit": 5.0,
        "contracts": 4,
        "entry_spot": 100.0,
        "target": 115.0,
        "stop": 95.0,
        "opened_at": (now - timedelta(days=3)).isoformat(),
        "status": "open",
        "peak_pnl_pct": 0.0,
        "scaled_out": [],
        "entry_context": {},
    }
    p.update(over)
    return p


@pytest.fixture
def eval_env(monkeypatch):
    """Stub every market lookup used by portfolio._evaluate_one.

    Returns a setter so a test can dictate the spot price and the option mark
    deterministically instead of depending on live quotes.
    """
    import portfolio as pf

    state = {"spot": 100.0, "mark": 5.0, "flip": None}

    monkeypatch.setattr(pf, "get_current_price", lambda t: state["spot"])
    monkeypatch.setattr(pf, "_spread_mark",
                        lambda *a, **k: state["mark"])
    monkeypatch.setattr(pf, "get_ohlcv", lambda *a, **k: None)
    monkeypatch.setattr(pf, "score_ticker", lambda *a, **k: state["flip"])
    return state


# ---------------------------------------------------------------- fake yfinance

class FakeChain:
    def __init__(self, calls, puts):
        self.calls = calls
        self.puts = puts


class FakeTicker:
    """Stand-in for yf.Ticker with a fixed option chain - keeps options tests offline."""

    def __init__(self, ticker, chain=None, expiries=None, spot=100.0):
        self.ticker = ticker
        self._chain = chain
        self.options = tuple(expiries or ())
        self._spot = spot

    def option_chain(self, expiry):
        if self._chain is None:
            raise ValueError("no chain")
        return self._chain

    def history(self, period="1d"):
        return pd.DataFrame({"Close": [self._spot]})


def option_rows(strikes, bid, ask, iv=0.30, oi=500, volume=100):
    """One row per strike; bid/ask may be scalars or per-strike callables."""
    def val(v, k):
        return float(v(k)) if callable(v) else float(v)
    return pd.DataFrame({
        "strike": [float(s) for s in strikes],
        "bid": [val(bid, s) for s in strikes],
        "ask": [val(ask, s) for s in strikes],
        "lastPrice": [(val(bid, s) + val(ask, s)) / 2 for s in strikes],
        "impliedVolatility": [iv] * len(strikes),
        "openInterest": [oi] * len(strikes),
        "volume": [volume] * len(strikes),
    })


def near_expiry(days=30):
    return (datetime.now().date() + timedelta(days=days)).isoformat()


@pytest.fixture
def neutral_context(monkeypatch):
    """Neutral IV / no earnings / aligned regime, so quality scoring is deterministic.

    assess_quality reaches out to yfinance through signals.context; without this the
    options tests would be network-dependent and their scores non-reproducible.
    """
    import signals.context as ctx
    monkeypatch.setattr(ctx, "iv_assessment", lambda t, iv: {
        "iv": iv, "rv": iv, "ratio": 1.0, "vol_pct": 50, "verdict": "fair",
        "expensive": False, "note": ""})
    monkeypatch.setattr(ctx, "earnings_check", lambda t, e, hold_days=10: {
        "date": None, "in_hold": False, "before_expiry": False})
    monkeypatch.setattr(ctx, "regime_alignment", lambda s: {
        "bias": 0.0, "regime": "neutral", "aligned": True, "against_trend": False})
    monkeypatch.setattr(ctx, "market_regime", lambda: {"bias": 0.0, "regime": "neutral"})
    return ctx
