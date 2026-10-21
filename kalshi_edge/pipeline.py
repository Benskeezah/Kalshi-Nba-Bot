"""Glue: market -> price-free spec -> estimate -> consistency -> compare -> rank -> log.

Order is the anti-anchoring guarantee: every estimate is produced from a
ContractSpec (no prices) and its p_yes/range are frozen before any price is
read. Later steps may only lower confidence or add explanations.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional

from .config import settings
from .consistency import check_event, check_prop_ladders
from .edge import ORDER, analyze
from .estimators.base import cap_confidence
from .estimators.registry import EstimatorRegistry
from .external.evidence_file import EvidenceFile
from .models import Analysis, Estimate, MarketSnapshot
from .store import PredictionStore

CONF_W = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.25, "NONE": 0.0}
LIQ_W = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.35, "NONE": 0.0}
MIN_RESOLVED_FOR_HIGH = 30


def estimate_market(m: MarketSnapshot, registry: EstimatorRegistry, evidence: EvidenceFile,
                    store: Optional[PredictionStore], now: Optional[datetime]) -> tuple[Estimate, int]:
    spec = m.contract_spec()                                  # no prices beyond this line
    est = registry.estimate(spec, evidence.for_market(m.ticker, m.event_ticker), now)
    resolved = store.resolved_count(est.estimator) if store else 0
    if not est.insufficient and resolved < MIN_RESOLVED_FOR_HIGH:
        capped = cap_confidence(est.confidence, "MEDIUM")
        if capped != est.confidence:
            est.assumptions.append(f"Confidence capped at MEDIUM: only {resolved} resolved predictions "
                                   f"for {est.estimator} (need {MIN_RESOLVED_FOR_HIGH}).")
            est.confidence = capped
    return est, resolved


def rank_score(a: Analysis, now: Optional[datetime] = None) -> float:
    """Ranking is NOT the raw edge. It is after-cost EV (mostly the pessimistic
    end of the range), discounted for confidence, liquidity, depth and time."""
    if a.verdict == "INSUFFICIENT DATA" or not a.best_side:
        return -1.0
    best = a.yes_cost if a.best_side == "YES" else a.no_cost
    cons = max(best.ev_net_conservative or 0.0, 0.0)
    central = max(best.ev_net or 0.0, 0.0)
    days = ((a.market.close_time - (now or a.market.fetched_at)).total_seconds() / 86400
            if a.market.close_time else 30)
    time_w = 1.0 if days <= 7 else 0.8 if days <= 30 else 0.6
    fill_w = 0.5 if best.fillable is False else 0.85 if best.fillable is None else 1.0
    return round((cons + 0.25 * central) * CONF_W[a.estimate.confidence]
                 * LIQ_W[a.liquidity_label] * time_w * fill_w * 100, 3)


def scan(markets: list[MarketSnapshot], registry: EstimatorRegistry, evidence: EvidenceFile,
         store: Optional[PredictionStore] = None, contracts: Optional[int] = None,
         now: Optional[datetime] = None) -> list[Analysis]:
    contracts = contracts or settings().default_size
    # 1. Estimate everything first, price-free.
    ests, resolved = {}, {}
    for m in markets:
        ests[m.ticker], resolved[m.ticker] = estimate_market(m, registry, evidence, store, now)
    # 2. Event-level consistency (may only lower confidence / add notes).
    by_event = defaultdict(list)
    for m in markets:
        by_event[m.event_ticker].append(m)
    for evt_markets in by_event.values():
        evt_ests = {m.ticker: ests[m.ticker] for m in evt_markets}
        if len(evt_markets) >= 4:
            check_event(evt_markets, evt_ests)
        check_prop_ladders(evt_markets, evt_ests)
    # 3. Compare with the market, rank, log.
    out = []
    for m in markets:
        a = analyze(m, ests[m.ticker], contracts, resolved[m.ticker], now)
        a.rank_score = rank_score(a, now)
        if store:
            store.log(a)
        out.append(a)
    # Verdict tier first, then the risk-adjusted score within a tier, so a
    # "NO CLEAR EDGE" never outranks a supported edge because its raw EV is bigger.
    tier = lambda a: ORDER.index(a.verdict) if a.verdict in ORDER[3:] else 0
    return sorted(out, key=lambda a: (tier(a), a.rank_score), reverse=True)


def analyze_market(m: MarketSnapshot, registry: EstimatorRegistry, evidence: EvidenceFile,
                   store: Optional[PredictionStore] = None, contracts: Optional[int] = None,
                   now: Optional[datetime] = None,
                   siblings: Optional[list[MarketSnapshot]] = None) -> Analysis:
    """Single market. Pass the other markets in its event as `siblings` so the
    consistency check can run; only the requested market is logged."""
    if siblings and len(siblings) >= 4:
        siblings = [m if s.ticker == m.ticker else s for s in siblings]  # keep m's order book
        results = scan(siblings, registry, evidence, None, contracts, now)
        a = next(x for x in results if x.market.ticker == m.ticker)
        if store:
            store.log(a)
        return a
    return scan([m], registry, evidence, store, contracts, now)[0]
