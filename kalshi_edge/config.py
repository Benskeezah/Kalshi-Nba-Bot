"""Configuration from environment variables. No credentials are hardcoded.

READ-ONLY: this package contains no order, cancel or portfolio-write code.
Public market data needs no API key; KALSHI_API_KEY_ID is read only so a
future authenticated (still read-only) client can use it.
"""
import os
from dataclasses import dataclass

READ_ONLY = True  # There is intentionally no code path that places trades.


@dataclass(frozen=True)
class Settings:
    kalshi_base: str = os.environ.get(
        "KALSHI_API_BASE", "https://external-api.kalshi.com/trade-api/v2")
    kalshi_key_id: str | None = os.environ.get("KALSHI_API_KEY_ID")  # unused for now
    # api.weather.gov asks every client to identify itself with contact info.
    nws_user_agent: str = os.environ.get(
        "NWS_USER_AGENT", "kalshi-edge-research (set NWS_USER_AGENT to your email)")
    db_path: str = os.environ.get("KALSHI_EDGE_DB", "predictions.sqlite")
    # Fee model (Kalshi schedule effective 2026-07-07): ceil(M * rate * C * P * (1-P))
    taker_fee_rate: float = float(os.environ.get("KALSHI_TAKER_FEE_RATE", "0.07"))
    fee_multiplier: float = float(os.environ.get("KALSHI_FEE_MULTIPLIER", "1.0"))
    # Size used for slippage/fee math when scanning (contracts).
    default_size: int = int(os.environ.get("KALSHI_EDGE_SIZE", "10"))
    http_timeout: float = float(os.environ.get("KALSHI_EDGE_TIMEOUT", "15"))


def settings() -> Settings:
    return Settings()
