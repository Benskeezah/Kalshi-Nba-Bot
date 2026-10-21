"""Parlay / combo markets (Kalshi KXMVE* multi-leg contracts).

A combo resolves YES only if every leg resolves YES. Each leg's probability
comes from (1) the evidence file, or (2) an automatic resolver such as NBA
game winners from de-vigged sportsbook lines. If any leg has neither, the
result is INSUFFICIENT DATA.

Legs from DIFFERENT games are treated as independent. Legs from the same game
(a team to win plus its star's points, say) are correlated, so the range is
widened and confidence stays LOW.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ..models import ContractSpec, Estimate, Evidence, utcnow
from .base import Estimator

# resolver(leg_market_ticker, side, now) -> (p, p_low, p_high, source, game_key, is_mock) or None
LegResolver = Callable[[str, str, datetime], Optional[tuple]]


def parse_legs(title: str) -> list[str]:
    return [leg.strip() for leg in (title or "").split(",") if leg.strip()]


class ParlayEstimator(Estimator):
    name = "parlay_product_v1"

    def __init__(self, resolvers: Optional[list[LegResolver]] = None,
                 joint_fns: Optional[list[Callable]] = None):
        self.resolvers = resolvers or []
        # joint_fn(list_of_leg_conditions) -> (p, lo, hi, note) or None, for legs in one game
        self.joint_fns = joint_fns or []

    def applies(self, spec: ContractSpec, evidence: dict) -> bool:
        return spec.series_ticker.startswith("KXMVE") or bool(spec.legs) or "legs" in evidence

    def estimate(self, spec: ContractSpec, evidence: dict,
                 now: Optional[datetime] = None) -> Estimate:
        now = now or utcnow()
        # Prefer Kalshi's own leg list (exact market tickers); fall back to the title text.
        legs = [f"{t} ({side})" for t, side in spec.legs] or parse_legs(spec.title)
        leg_ids = {f"{t} ({side})": (t, side) for t, side in spec.legs}
        if not legs:
            return Estimate.insufficient_data(self.name, "Could not parse parlay legs from the title.")
        given = evidence.get("legs", {})
        file_mock = bool(evidence.get("_mock"))
        resolved, missing = [], []
        for leg in legs:
            if leg in given:
                g = given[leg]
                q = float(g["p"])
                resolved.append((leg, q, float(g.get("p_low", max(0.0, q - 0.05))),
                                 float(g.get("p_high", min(1.0, q + 0.05))), g["source"],
                                 g.get("game"), file_mock, None))
                continue
            r = None
            if leg in leg_ids:
                for fn in self.resolvers:
                    try:
                        r = fn(*leg_ids[leg], now)
                    except Exception:
                        r = None
                    if r:
                        break
            if r:
                resolved.append((leg, *r[:6], r[6] if len(r) > 6 else None))
            else:
                missing.append(leg)
        if missing:
            return Estimate.insufficient_data(
                self.name, f"No sourced probability for {len(missing)} of {len(legs)} legs: " + "; ".join(missing),
                [Evidence(src, f"Leg '{leg}': {q:.0%}", q, None, None, mk)
                 for leg, q, _, _, src, _, mk, _ in resolved])

        evs, games = [], []
        for leg, q, ql, qh, src, game, mk, _ in resolved:
            evs.append(Evidence(src, f"Leg '{leg}': {q:.1%}", q, None, None, mk))
            games.append(game)
        # Group legs by game. Different games multiply; same-game legs use a joint
        # model when one exists, otherwise they multiply and are flagged correlated.
        groups: dict = {}
        for i, r in enumerate(resolved):
            groups.setdefault(r[5] or f"_leg{i}", []).append(r)
        p = lo = hi = 1.0
        joint_notes, unhandled_same_game = [], False
        for key, rs in groups.items():
            if len(rs) > 1:
                j = None
                for fn in self.joint_fns:
                    j = fn([r[7] for r in rs])
                    if j:
                        break
                if j:
                    p, lo, hi = p * j[0], lo * j[1], hi * j[2]
                    joint_notes.append(f"Same-game legs ({key}): {j[3]}.")
                    if len(j) > 4 and not j[4]:          # joint was only partial
                        unhandled_same_game = True
                    continue
                unhandled_same_game = True
            for r in rs:
                p, lo, hi = p * r[1], lo * r[2], hi * r[3]
        known = [g for g in games if g]
        all_known = len(known) == len(games)
        same_game = len(known) != len(set(known))
        auto_independent = all_known and not unhandled_same_game
        independent = bool(evidence.get("legs_independent")) or auto_independent
        if not independent:
            lo, hi = lo * 0.85, min(1.0, hi * 1.25)
        mock = any(e.is_mock for e in evs)
        conf = "MEDIUM" if independent and len(legs) <= 3 and not mock and not same_game else "LOW"
        opposing = [f"Errors compound across {len(legs)} legs: small misses on each leg add up."]
        if unhandled_same_game:
            opposing.append("Two or more legs are from the same game, so they are correlated.")
        elif same_game:
            opposing.append("Same-game legs handled with a joint model, which rests on extra assumptions.")
        elif not independent:
            opposing.append("Leg independence is unverified; the product could be badly off.")
        return Estimate(
            self.name, p, lo, hi, conf, evs,
            supporting=[f"Combined probability of {len(legs)} legs = {p:.1%}."]
                       + (["Legs in different games treated as independent."] if auto_independent else [])
                       + joint_notes,
            opposing=opposing,
            assumptions=["Each leg's probability is accurate and current.",
                         "Legs are independent." if independent else
                         "Independence NOT assumed; range widened for correlation."],
            would_change=["Injury or lineup news on any leg.", "Line moves on any leg."],
            time_sensitive=True, created_at=now)
