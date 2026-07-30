"""
Central configuration for the Trader backend.

Everything that might reasonably differ between a laptop, a container and a server
lives here, read from the environment with a working default. Nothing in this module
imports application code, so it is safe to import from anywhere (including at module
scope in main.py) without creating a cycle.

Nothing here is wired into the application yet -- this module is deliberately
side-effect free apart from loading a .env file. See README.md ("Wiring config.py
into the app") for the exact call sites.

Usage:
    from config import settings
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

Precedence: real environment variable > backend/.env > .env at repo root > default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# --- paths ---------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent

# --- .env loading ---------------------------------------------------------------
# python-dotenv is already a dependency. It is optional here so that config.py still
# imports in a bare container where only stdlib is present.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(BACKEND_DIR / ".env")
    load_dotenv(REPO_ROOT / ".env")
except Exception:  # pragma: no cover
    pass


# --- env helpers ----------------------------------------------------------------
def _str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _str(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of configuration, resolved at import time."""

    # ---- HTTP server ----------------------------------------------------------
    api_host: str = field(default_factory=lambda: _str("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: _int("API_PORT", 8000))
    reload: bool = field(default_factory=lambda: _bool("API_RELOAD", False))

    # Comma-separated list. Must be explicit origins (not "*") because the app
    # sends credentials, and CORS forbids wildcard + credentials together.
    cors_origins: list[str] = field(
        default_factory=lambda: _list(
            "CORS_ORIGINS", ["http://localhost:3000", "http://127.0.0.1:3000"]
        )
    )

    # ---- logging --------------------------------------------------------------
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())

    # ---- file paths -----------------------------------------------------------
    # Trade history. Mounted as a volume under Docker so it survives rebuilds.
    portfolio_file: Path = field(
        default_factory=lambda: Path(
            _str("PORTFOLIO_FILE", str(BACKEND_DIR / "portfolio.json"))
        ).expanduser()
    )

    # ---- data cache TTLs (seconds) --------------------------------------------
    # Current hard-coded equivalents: data/fetcher.py CACHE_TTL=900,
    # data/news.py CACHE_TTL=1800, signals/context.py _TTL=21600.
    price_cache_ttl: int = field(default_factory=lambda: _int("PRICE_CACHE_TTL", 900))
    news_cache_ttl: int = field(default_factory=lambda: _int("NEWS_CACHE_TTL", 1800))
    context_cache_ttl: int = field(
        default_factory=lambda: _int("CONTEXT_CACHE_TTL", 6 * 3600)
    )
    regime_cache_ttl: int = field(default_factory=lambda: _int("REGIME_CACHE_TTL", 900))

    # ---- scanning -------------------------------------------------------------
    scan_on_startup: bool = field(default_factory=lambda: _bool("SCAN_ON_STARTUP", True))
    scan_interval_minutes: int = field(
        default_factory=lambda: _int("SCAN_INTERVAL_MINUTES", 30)
    )
    momentum_scan_limit: int = field(
        default_factory=lambda: _int("MOMENTUM_SCAN_LIMIT", 120)
    )
    scan_max_workers: int = field(default_factory=lambda: _int("SCAN_MAX_WORKERS", 12))

    # ---- account / risk defaults ----------------------------------------------
    # Mirrors of the constants in risk.py and portfolio.py. Provided so the numbers
    # can be tuned without editing source; risk.py is NOT reading them yet.
    starting_equity: float = field(
        default_factory=lambda: _float("STARTING_EQUITY", 600.0)
    )
    base_risk_pct: float = field(default_factory=lambda: _float("BASE_RISK_PCT", 0.02))
    min_risk_pct: float = field(default_factory=lambda: _float("MIN_RISK_PCT", 0.005))
    max_portfolio_heat: float = field(
        default_factory=lambda: _float("MAX_PORTFOLIO_HEAT", 0.06)
    )
    max_open_positions: int = field(
        default_factory=lambda: _int("MAX_OPEN_POSITIONS", 4)
    )
    dd_halve_size: float = field(default_factory=lambda: _float("DD_HALVE_SIZE", 0.10))
    dd_stop_trading: float = field(
        default_factory=lambda: _float("DD_STOP_TRADING", 0.20)
    )
    consecutive_loss_pause: int = field(
        default_factory=lambda: _int("CONSECUTIVE_LOSS_PAUSE", 4)
    )
    typical_min_contract_cost: float = field(
        default_factory=lambda: _float("TYPICAL_MIN_CONTRACT_COST", 100.0)
    )

    # ---- exit rules (portfolio.py mirrors) ------------------------------------
    take_profit_pct: float = field(default_factory=lambda: _float("TAKE_PROFIT_PCT", 0.70))
    stop_loss_pct: float = field(default_factory=lambda: _float("STOP_LOSS_PCT", 0.40))
    time_stop_dte: int = field(default_factory=lambda: _int("TIME_STOP_DTE", 14))
    max_hold_days: int = field(default_factory=lambda: _int("MAX_HOLD_DAYS", 12))

    # ---- backtest cost model (walkforward.py mirrors) -------------------------
    commission_per_contract: float = field(
        default_factory=lambda: _float("COMMISSION_PER_CONTRACT", 0.65)
    )
    slippage_frac_of_price: float = field(
        default_factory=lambda: _float("SLIPPAGE_FRAC_OF_PRICE", 0.02)
    )

    # ---- optional third-party keys --------------------------------------------
    # Both are optional; the app degrades to template reasons / Yahoo headlines.
    anthropic_api_key: str = field(
        default_factory=lambda: _str("ANTHROPIC_API_KEY", "")
    )
    anthropic_model: str = field(
        default_factory=lambda: _str("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    )
    news_api_key: str = field(default_factory=lambda: _str("NEWS_API_KEY", ""))

    # ---- derived --------------------------------------------------------------
    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def news_api_enabled(self) -> bool:
        return bool(self.news_api_key)

    def safe_dict(self) -> dict:
        """Config as a dict with secrets redacted -- safe to log or expose."""
        d = asdict(self)
        d["portfolio_file"] = str(self.portfolio_file)
        for k in ("anthropic_api_key", "news_api_key"):
            d[k] = "***set***" if d[k] else ""
        return d


settings = Settings()


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(settings.safe_dict(), indent=2, sort_keys=True))
