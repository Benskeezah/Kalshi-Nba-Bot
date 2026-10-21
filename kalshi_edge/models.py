"""Core data types shared by every module.

Design rule: estimators only ever see a ContractSpec, which deliberately has
NO price, volume or order-book fields. That is how the system keeps the market
price from anchoring the independent probability estimate.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Market side (prices live here, and only here)
# --------------------------------------------------------------------------
@dataclass
class OrderbookLevel:
    price: float                # dollars, 0-1
    quantity: Optional[float]   # contracts; None = depth unknown


@dataclass
class MarketSnapshot:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_sub_title: str
    rules_primary: str
    rules_secondary: str
    status: str
    close_time: Optional[datetime]
    strike_type: Optional[str]
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    last_price: Optional[float]
    previous_price: Optional[float]
    volume: float
    volume_24h: float
    open_interest: float
    fetched_at: datetime
    source: str                       # "kalshi-api" or "fixture:<path>"
    result: Optional[str] = None      # "yes" / "no" once settled
    legs: tuple = ()                  # parlay legs: ((market_ticker, side), ...)
    # Executable depth. Buying YES consumes these levels (derived from NO bids).
    yes_ask_levels: list[OrderbookLevel] = field(default_factory=list)
    no_ask_levels: list[OrderbookLevel] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def mid(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        if self.yes_ask <= 0 or self.yes_bid >= 1:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 4)

    def contract_spec(self) -> "ContractSpec":
        """Price-free view handed to estimators."""
        return ContractSpec(
            ticker=self.ticker,
            event_ticker=self.event_ticker,
            series_ticker=self.series_ticker,
            title=self.title,
            yes_sub_title=self.yes_sub_title,
            rules_primary=self.rules_primary,
            rules_secondary=self.rules_secondary,
            close_time=self.close_time,
            strike_type=self.strike_type,
            floor_strike=self.floor_strike,
            cap_strike=self.cap_strike,
            legs=self.legs,
        )


@dataclass(frozen=True)
class ContractSpec:
    """What the contract IS. Intentionally contains no market prices."""
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_sub_title: str
    rules_primary: str
    rules_secondary: str
    close_time: Optional[datetime]
    strike_type: Optional[str]
    floor_strike: Optional[float]
    cap_strike: Optional[float]
    legs: tuple = ()                  # parlay legs: ((market_ticker, side), ...)


# --------------------------------------------------------------------------
# Evidence and estimates
# --------------------------------------------------------------------------
@dataclass
class Evidence:
    source: str              # e.g. "NWS point forecast (forecast.weather.gov)"
    description: str         # what it says, in plain words
    value: Optional[float] = None
    observed_at: Optional[datetime] = None   # when the data was issued
    url: Optional[str] = None
    is_mock: bool = False    # True for test/demo data that is NOT real

    def to_dict(self) -> dict:
        d = asdict(self)
        d["observed_at"] = self.observed_at.isoformat() if self.observed_at else None
        return d


@dataclass
class Estimate:
    """Output of an estimator. p_yes is None when data is insufficient."""
    estimator: str
    p_yes: Optional[float]
    p_low: Optional[float]
    p_high: Optional[float]
    confidence: str                    # HIGH / MEDIUM / LOW / NONE
    evidence: list[Evidence] = field(default_factory=list)
    supporting: list[str] = field(default_factory=list)
    opposing: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    would_change: list[str] = field(default_factory=list)
    insufficient_reason: Optional[str] = None
    time_sensitive: bool = False
    created_at: datetime = field(default_factory=utcnow)
    params: dict = field(default_factory=dict)   # model parameters (e.g. mu/sigma) for diagnostics

    @property
    def insufficient(self) -> bool:
        return self.p_yes is None

    @classmethod
    def insufficient_data(cls, estimator: str, reason: str,
                          evidence: Optional[list[Evidence]] = None) -> "Estimate":
        return cls(estimator=estimator, p_yes=None, p_low=None, p_high=None,
                   confidence="NONE", evidence=evidence or [],
                   insufficient_reason=reason)


# --------------------------------------------------------------------------
# Comparison / costs / verdict
# --------------------------------------------------------------------------
@dataclass
class SideCost:
    side: str                 # "YES" or "NO"
    contracts: int
    avg_fill: Optional[float] # dollars per contract incl. slippage, None if no liquidity
    top_of_book: Optional[float]
    slippage: float           # avg_fill - top_of_book
    fee_per_contract: float
    fillable: bool            # enough depth for the requested size?
    ev_gross: Optional[float] # model_p_side - top_of_book
    ev_net: Optional[float]   # model_p_side - avg_fill - fee
    ev_net_conservative: Optional[float]  # using the pessimistic end of the range


@dataclass
class Analysis:
    market: MarketSnapshot
    estimate: Estimate
    market_prob: Optional[float]        # mid-price implied probability
    edge_pp: Optional[float]            # (model - market) * 100
    yes_cost: Optional[SideCost]
    no_cost: Optional[SideCost]
    best_side: Optional[str]
    contrarian: list[str]
    verdict: str
    verdict_reason: str
    liquidity_label: str
    rank_score: float = 0.0
