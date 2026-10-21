"""Transaction costs: Kalshi fees, spread and slippage.

Fee (Kalshi schedule effective 2026-07-07, taker orders):
    fee = ceil_to_cent( M * 0.07 * C * P * (1 - P) )
where C = contracts, P = price in dollars, M = series multiplier (default 1).
There is no fee at settlement. Rate and multiplier come from env vars so a
schedule change is a config edit, not a code change. Assumes taker orders,
which is the conservative case for analysis.
"""
from __future__ import annotations

import math
from typing import Optional

from .config import settings
from .models import OrderbookLevel, SideCost


def taker_fee_total(price: float, contracts: int, rate: Optional[float] = None,
                    multiplier: Optional[float] = None) -> float:
    s = settings()
    rate = s.taker_fee_rate if rate is None else rate
    multiplier = s.fee_multiplier if multiplier is None else multiplier
    raw = multiplier * rate * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


def fill_price(levels: list[OrderbookLevel], contracts: int) -> tuple[Optional[float], Optional[bool]]:
    """Average price to buy `contracts` by walking the asks.
    Returns (avg_price, fillable). fillable is None when depth is unknown."""
    if not levels:
        return None, False
    if levels[0].quantity is None:
        return levels[0].price, None
    remaining, cost = contracts, 0.0
    for lvl in sorted(levels, key=lambda l: l.price):
        take = min(remaining, lvl.quantity or 0)
        cost += take * lvl.price
        remaining -= take
        if remaining <= 0:
            return cost / contracts, True
    filled = contracts - remaining
    return (cost / filled if filled else levels[0].price), False


def side_cost(side: str, levels: list[OrderbookLevel], p_side: float,
              p_side_conservative: float, contracts: int) -> SideCost:
    top = levels[0].price if levels else None
    avg, fillable = fill_price(levels, contracts)
    if avg is None:
        return SideCost(side, contracts, None, None, 0.0, 0.0, False, None, None, None)
    fee = taker_fee_total(avg, contracts) / contracts
    return SideCost(
        side=side, contracts=contracts, avg_fill=round(avg, 4), top_of_book=top,
        slippage=round(avg - top, 4), fee_per_contract=round(fee, 4), fillable=fillable,
        ev_gross=round(p_side - top, 4),
        ev_net=round(p_side - avg - fee, 4),
        ev_net_conservative=round(p_side_conservative - avg - fee, 4),
    )
