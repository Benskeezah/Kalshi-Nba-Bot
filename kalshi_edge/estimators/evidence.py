"""Generic estimator for any market, driven by an evidence file.

Combines, in log-odds space:
  * a base rate (successes / trials) with a Jeffreys Beta posterior,
  * independent probability estimates (devigged sportsbook lines, other
    forecasters, comparable markets - NOT the Kalshi price itself),
  * explicit, sourced adjustments.
With no usable inputs it returns INSUFFICIENT DATA.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from ..models import ContractSpec, Estimate, Evidence, utcnow
from .base import Estimator, beta_interval, inv_logit, logit


def _ts(s):
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class EvidenceEstimator(Estimator):
    name = "evidence_blend_v1"

    def applies(self, spec: ContractSpec, evidence: dict) -> bool:
        return any(k in evidence for k in ("base_rate", "estimates"))

    def estimate(self, spec: ContractSpec, evidence: dict,
                 now: Optional[datetime] = None) -> Estimate:
        now = now or utcnow()
        mock = bool(evidence.get("_mock"))
        comps = []          # (logit_mean, logit_halfwidth, weight)
        evs, supporting, opposing, assumptions = [], [], [], []

        br = evidence.get("base_rate")
        if br:
            s, n = float(br["successes"]), float(br["trials"])
            if n <= 0 or not 0 <= s <= n:
                return Estimate.insufficient_data(self.name, "Base rate has invalid counts.")
            a, b = s + 0.5, n - s + 0.5
            mean = a / (a + b)
            lo, hi = beta_interval(a, b, 0.8)
            comps.append((logit(mean), (logit(hi) - logit(lo)) / 2, float(br.get("weight", 1))))
            evs.append(Evidence(br["source"], f"Base rate {int(s)}/{int(n)} = {s/n:.0%}",
                                s / n, _ts(br.get("as_of")), br.get("url"), mock))
            supporting.append(f"Historical base rate {int(s)}/{int(n)} ({s/n:.0%}).")
            if n < 30:
                opposing.append(f"Base-rate sample is small (n={int(n)}).")
            assumptions.append("Past cases in the base rate are comparable to this one.")

        for e in evidence.get("estimates", []):
            p = float(e["p"])
            hw = float(e.get("logit_halfwidth", 0.35))   # default ~+-8pp around 50%
            comps.append((logit(p), hw, float(e.get("weight", 1))))
            evs.append(Evidence(e["source"], f"Independent estimate {p:.0%}", p,
                                _ts(e.get("as_of")), e.get("url"), mock))
            supporting.append(f"{e['source']}: {p:.0%}.")

        if not comps:
            return Estimate.insufficient_data(self.name, "No base rate or independent estimate provided.")

        wsum = sum(w for _, _, w in comps)
        center = sum(m * w for m, _, w in comps) / wsum
        spread_between = math.sqrt(sum(w * (m - center) ** 2 for m, _, w in comps) / wsum)
        half = max(sum(h * w for _, h, w in comps) / wsum, spread_between)

        shift, shift_unc = 0.0, 0.0
        for adj in evidence.get("adjustments", []):
            if not adj.get("source"):
                opposing.append(f"Ignored unsourced adjustment: {adj.get('reason', '?')}.")
                continue
            d = float(adj["log_odds"])
            shift += d
            shift_unc += abs(d) * 0.5
            evs.append(Evidence(adj["source"], f"Adjustment {d:+.2f} log-odds: {adj['reason']}",
                                d, _ts(adj.get("as_of")), adj.get("url"), mock))
            (supporting if d > 0 else opposing).append(f"Adjustment: {adj['reason']} ({d:+.2f} log-odds).")
            assumptions.append(f"Size of adjustment '{adj['reason']}' is a judgment call.")

        c = center + shift
        p, p_lo, p_hi = inv_logit(c), inv_logit(c - half - shift_unc), inv_logit(c + half + shift_unc)

        n_ind = len(comps)
        width = p_hi - p_lo
        conf = "MEDIUM" if n_ind >= 2 and width <= 0.25 else "LOW"
        if spread_between > 0.6:
            conf = "LOW"
            opposing.append("Independent sources disagree substantially.")
        if mock:
            conf = "LOW"
            assumptions.append("Evidence is MOCK demo data, not real research.")
        assumptions.append("Generic blend never rates above MEDIUM until calibrated.")

        dates = [e.observed_at for e in evs if e.observed_at]
        if dates and (now - max(dates)).days > 7:
            opposing.append(f"Newest evidence is {(now - max(dates)).days} days old.")
        return Estimate(self.name, p, p_lo, p_hi, conf, evs, supporting, opposing, assumptions,
                        would_change=["Any new primary-source information (injuries, polls, releases).",
                                      "A different, larger base-rate sample."],
                        time_sensitive=bool(spec.close_time and (spec.close_time - now).days < 2),
                        created_at=now)
