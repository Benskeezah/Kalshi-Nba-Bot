"""Event-level consistency checks for bucketed markets (e.g. daily highs).

Buckets in one event are pieces of a single distribution. If the model
disagrees with every bucket, the cause is usually ONE assumption, not many
independent mispricings. This module fits the distribution the market prices
imply and reports whether the disagreement is about the forecast centre (mu)
or the uncertainty width (sigma).

It runs AFTER the estimates are frozen. It never changes p_yes or its range;
it only lowers confidence and adds explanations, because the model's sigma is
its least-verified input until calibration data exists.
"""
from __future__ import annotations

from typing import Optional

from .estimators.base import lower_confidence
from .estimators.weather import bucket_probability
from .models import Estimate, MarketSnapshot


def implied_normal(markets: list[MarketSnapshot]) -> Optional[tuple[float, float, float]]:
    """Least-squares fit of (mu, sigma) to normalized bucket mids.
    Returns (mu, sigma, overround) or None if the event can't be fitted."""
    pts = [(m, m.mid) for m in markets if m.mid is not None]
    if len(pts) < 4:
        return None
    total = sum(p for _, p in pts)
    if total <= 0:
        return None
    target = [(m.contract_spec(), p / total) for m, p in pts]
    lo = min((s.floor_strike or s.cap_strike) for s, _ in target) - 5
    hi = max((s.cap_strike or s.floor_strike) for s, _ in target) + 5
    best = None
    mu = lo
    while mu <= hi:
        sig = 0.6
        while sig <= 8.0:
            err = 0.0
            for s, p in target:
                q = bucket_probability(s, mu, sig)
                err += (q - p) ** 2
            if best is None or err < best[0]:
                best = (err, mu, sig)
            sig += 0.05
        mu += 0.1
    return best[1], best[2], total - 1.0


def check_event(markets: list[MarketSnapshot], ests: dict[str, Estimate]) -> list[str]:
    """Mutates ests in place (confidence + notes). Returns event-level notes."""
    params = [e.params for e in ests.values() if not e.insufficient and "sigma" in e.params]
    if not params:
        return []
    fit = implied_normal(markets)
    if not fit:
        return []
    mkt_mu, mkt_sig, overround = fit
    mod_mu, mod_sig = params[0]["mu"], params[0]["sigma"]
    notes = [f"Market prices imply a high of ~{mkt_mu:.1f}°F with SD ~{mkt_sig:.1f}°F "
             f"(model: {mod_mu:.1f}°F, SD {mod_sig:.1f}°F). Bucket prices sum to "
             f"{(1+overround)*100:.0f}%."]
    ratio = mod_sig / mkt_sig
    width_driven = abs(mod_mu - mkt_mu) < 1.0 and (ratio > 1.35 or ratio < 0.74)
    if width_driven:
        notes.append("The disagreement is mostly about uncertainty WIDTH, not the forecast itself. "
                     "Every bucket 'edge' in this event is the same single bet on that assumption.")
    elif abs(mod_mu - mkt_mu) >= 1.0:
        notes.append(f"The market's centre is {mkt_mu - mod_mu:+.1f}°F from the model's. It may be "
                     "using a newer forecast; check before trusting any bucket edge.")
    for t, e in ests.items():
        if e.insufficient or "sigma" not in e.params:
            continue
        m = next(x for x in markets if x.ticker == t)
        alt = bucket_probability(m.contract_spec(), mod_mu, mkt_sig)
        e.opposing.append(f"With the market's implied SD ({mkt_sig:.1f}°F) instead of the model's "
                          f"({mod_sig:.1f}°F), this bucket would be {alt:.0%}.")
        e.opposing.extend(notes)
        if width_driven or abs(mod_mu - mkt_mu) >= 1.0:
            e.confidence = lower_confidence(e.confidence)
            e.assumptions.append("Confidence lowered: the edge rests on an assumption the market disagrees "
                                 "with and that has not been calibrated yet.")
    return notes


# ---------------------------------------------------------------------------
# Player-prop ladders ("Durant 15+ / 20+ / 25+ / 30+")
# ---------------------------------------------------------------------------
def check_prop_ladders(markets: list[MarketSnapshot], ests: dict[str, Estimate]) -> None:
    """Fit the mean and spread the market's ladder implies for each player/stat and
    say whether any disagreement is about the player's expected output (a real
    view) or only about the spread of outcomes (an untested model assumption)."""
    from .estimators.nba_props import nb_sf
    groups: dict = {}
    for m in markets:
        e = ests.get(m.ticker)
        if e and not e.insufficient and e.params.get("kind") == "prop" and m.mid is not None:
            groups.setdefault((e.params["player"], e.params["stat"]), []).append((m, e))
    for (player, stat), rows in groups.items():
        if len(rows) < 3:
            continue
        mu0, r0 = rows[0][1].params["mu"], rows[0][1].params["r"]
        best = None
        for i in range(-40, 41):
            mu = mu0 * (1 + i / 100)
            for r in (2, 3, 5, 8, 12, 18, 25, 40, 60, 100, 200):
                err = sum((nb_sf(e.params["k"], mu, r) - m.mid) ** 2 for m, e in rows)
                if best is None or err < best[0]:
                    best = (err, mu, r)
        _, mu_m, r_m = best
        mean_gap = (mu_m - mu0) / mu0
        biggest_gap = max(abs(e.p_yes - m.mid) for m, e in rows)
        shape_only = (abs(mean_gap) < 0.05 and (r_m / r0 > 2 or r_m / r0 < 0.5) and biggest_gap >= 0.04)
        note = (f"Kalshi's {player} {stat} ladder implies an expected {mu_m:.1f} "
                f"(model {mu0:.1f}, {mean_gap:+.0%}).")
        for m, e in rows:
            e.opposing.append(note)
            if shape_only:
                e.opposing.append("Model and market agree on the expected value; the gap comes from how "
                                  "spread-out outcomes are assumed to be, which is not yet calibrated.")
                e.confidence = lower_confidence(e.confidence)
