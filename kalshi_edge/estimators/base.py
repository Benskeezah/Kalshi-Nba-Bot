"""Estimator interface and small stats helpers (stdlib only)."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..models import ContractSpec, Estimate

CONF_ORDER = ["NONE", "LOW", "MEDIUM", "HIGH"]


def cap_confidence(conf: str, cap: str) -> str:
    return conf if CONF_ORDER.index(conf) <= CONF_ORDER.index(cap) else cap


def lower_confidence(conf: str, steps: int = 1) -> str:
    i = max(1, CONF_ORDER.index(conf) - steps) if conf != "NONE" else 0
    return CONF_ORDER[i]


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def inv_logit(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def beta_interval(a: float, b: float, mass: float = 0.8, n: int = 4000) -> tuple[float, float]:
    """Central credible interval of Beta(a, b) by numeric integration."""
    xs = [(i + 0.5) / n for i in range(n)]
    logc = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    pdf = [math.exp(logc + (a - 1) * math.log(x) + (b - 1) * math.log(1 - x)) for x in xs]
    total = sum(pdf)
    lo_t, hi_t = (1 - mass) / 2, 1 - (1 - mass) / 2
    acc, lo, hi = 0.0, xs[0], xs[-1]
    for x, d in zip(xs, pdf):
        prev = acc
        acc += d / total
        if prev < lo_t <= acc:
            lo = x
        if prev < hi_t <= acc:
            hi = x
            break
    return lo, hi


class Estimator(ABC):
    """Every estimator receives a ContractSpec, which carries NO prices."""
    name: str = "base"

    @abstractmethod
    def applies(self, spec: ContractSpec, evidence: dict) -> bool: ...

    @abstractmethod
    def estimate(self, spec: ContractSpec, evidence: dict,
                 now: Optional[datetime] = None) -> Estimate: ...
