"""Market comparison, contrarian checks and verdicts.

This module is the ONLY place where the model estimate meets the market price,
and it runs after the estimate has been produced and frozen.

Verdict rules (screening guidelines, applied in order):
  1. No estimate                                   -> INSUFFICIENT DATA
  2. No executable quotes                          -> INSUFFICIENT DATA
  3. |edge| < 2pp                                  -> MARKET APPEARS FAIR
  4. Market inside the model's own range           -> MARKET APPEARS FAIR (<5pp) / NO CLEAR EDGE
  5. Best side's EV after fees+slippage <= 0.5c    -> NO CLEAR EDGE
  6. EV at the pessimistic end of the range <= 0   -> SMALL EDGE
  7. Otherwise:
       HIGH confidence, |edge| >= 5pp, decent liquidity, spread <= 4c  -> STRONG POTENTIAL EDGE
       MEDIUM+ confidence, |edge| >= 5pp                               -> POTENTIAL EDGE
       else                                                            -> SMALL EDGE
  Caps: LOW confidence -> at most SMALL EDGE; LOW liquidity or |edge| >= 15pp
        -> at most POTENTIAL EDGE (big gaps are more often model error).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .costs import side_cost
from .models import Analysis, Estimate, MarketSnapshot, utcnow

ORDER = ["INSUFFICIENT DATA", "NO CLEAR EDGE", "MARKET APPEARS FAIR",
         "SMALL EDGE", "POTENTIAL EDGE", "STRONG POTENTIAL EDGE"]


def liquidity_label(m: MarketSnapshot) -> str:
    if m.yes_ask is None and m.no_ask is None:
        return "NONE"
    sp = m.spread if m.spread is not None else 1.0
    if m.volume_24h >= 1000 and m.open_interest >= 1000 and sp <= 0.02:
        return "HIGH"
    if m.open_interest >= 200 and sp <= 0.05:
        return "MEDIUM"
    return "LOW"


def _cap(verdict: str, cap: str) -> str:
    if verdict in ("INSUFFICIENT DATA", "NO CLEAR EDGE", "MARKET APPEARS FAIR"):
        return verdict
    return verdict if ORDER.index(verdict) <= ORDER.index(cap) else cap


def contrarian_checks(m: MarketSnapshot, est: Estimate, market_prob: Optional[float],
                      edge_pp: Optional[float], liq: str, resolved_count: int,
                      now: datetime) -> list[str]:
    out = []
    if edge_pp is not None and abs(edge_pp) >= 10:
        out.append(f"The gap is {abs(edge_pp):.0f}pp. Gaps this large are more often a model error, "
                   "a stale input or a rules misread than a market error. Re-check data and rules first.")
    if m.spread is not None and m.spread > 0.04:
        out.append(f"Spread is {m.spread*100:.0f}c, so the midpoint is a noisy read of the market's view.")
    if liq in ("LOW", "NONE"):
        out.append("Thin market: few traders set this price, so it may be wrong, but you may "
                   "also be trading against one informed participant.")
    if est.time_sensitive:
        out.append("Probability is highly time-sensitive. Active traders may be acting on information "
                   "newer than this model's inputs (injury news, line moves, model runs, live data).")
    if resolved_count < 30:
        out.append(f"This estimator has {resolved_count} resolved predictions logged, so its error "
                   "rates are not yet verified; the market's collective track record is longer.")
    if market_prob is not None and (market_prob < 0.08 or market_prob > 0.92):
        out.append("Price is near 0 or 100: tick size and fees distort the implied probability, "
                   "and longshot pricing biases are common here.")
    if m.previous_price and m.last_price and abs(m.last_price - m.previous_price) >= 0.10:
        out.append(f"Price moved {m.previous_price*100:.0f}c -> {m.last_price*100:.0f}c recently; "
                   "something may have changed that the model has not seen.")
    if m.close_time and (m.close_time - now).total_seconds() < 6 * 3600:
        out.append("Closes within 6 hours: little time for the estimate to be wrong slowly, "
                   "a lot of room for late information.")
    return out


def analyze(m: MarketSnapshot, est: Estimate, contracts: int,
            resolved_count: int = 0, now: Optional[datetime] = None) -> Analysis:
    now = now or utcnow()
    liq = liquidity_label(m)
    market_prob = m.mid if m.mid is not None else None

    if est.insufficient:
        return Analysis(m, est, market_prob, None, None, None, None, [],
                        "INSUFFICIENT DATA", est.insufficient_reason or "", liq)

    edge_pp = (est.p_yes - market_prob) * 100 if market_prob is not None else None
    yes = side_cost("YES", m.yes_ask_levels, est.p_yes, est.p_low, contracts) if m.yes_ask_levels else None
    no = side_cost("NO", m.no_ask_levels, 1 - est.p_yes, 1 - est.p_high, contracts) if m.no_ask_levels else None
    sides = [s for s in (yes, no) if s and s.ev_net is not None]
    best = max(sides, key=lambda s: s.ev_net) if sides else None
    contra = contrarian_checks(m, est, market_prob, edge_pp, liq, resolved_count, now)

    def done(v, why):
        return Analysis(m, est, market_prob, edge_pp, yes, no, best.side if best else None,
                        contra, v, why, liq)

    if market_prob is None or best is None:
        return done("INSUFFICIENT DATA", "No two-sided quote to compare against or trade at.")
    ae = abs(edge_pp)
    if ae < 2:
        return done("MARKET APPEARS FAIR", f"Model and market differ by {ae:.1f}pp, within noise.")
    if est.p_low <= market_prob <= est.p_high:
        v = "MARKET APPEARS FAIR" if ae < 5 else "NO CLEAR EDGE"
        return done(v, f"Market's {market_prob:.0%} sits inside the model's own range "
                       f"({est.p_low:.0%}-{est.p_high:.0%}).")
    if best.ev_net <= 0.005:
        return done("NO CLEAR EDGE", f"After spread, slippage and fees the best side ({best.side}) "
                                     f"is worth {best.ev_net*100:+.1f}c per contract.")
    if best.ev_net_conservative <= 0:
        return done("SMALL EDGE", f"{best.side} shows {best.ev_net*100:+.1f}c/contract after costs at the "
                                  "central estimate, but loses money at the pessimistic end of the range.")
    if est.confidence == "HIGH" and ae >= 5 and liq in ("HIGH", "MEDIUM") and (m.spread or 1) <= 0.04:
        v = "STRONG POTENTIAL EDGE"
    elif est.confidence in ("HIGH", "MEDIUM") and ae >= 5:
        v = "POTENTIAL EDGE"
    else:
        v = "SMALL EDGE"
    if est.confidence == "LOW":
        v = _cap(v, "SMALL EDGE")
    if liq in ("LOW", "NONE") or ae >= 15:
        v = _cap(v, "POTENTIAL EDGE")
    if best.fillable is False:
        v = _cap(v, "SMALL EDGE")
    why = (f"{best.side} at {best.avg_fill*100:.0f}c: {best.ev_net*100:+.1f}c/contract after costs "
           f"({best.ev_net_conservative*100:+.1f}c at the pessimistic end); confidence {est.confidence}, "
           f"liquidity {liq}.")
    return done(v, why)
