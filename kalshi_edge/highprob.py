"""High-probability mode: the most likely winners that are still fairly priced.

Win rate alone is easy to maximise and easy to lose money with: buying a 95c
contract "wins" 95% of the time when priced fairly, but you need a win rate
above price + fee just to break even, and one loss wipes out ~19 wins.

So a pick qualifies only if ALL of these hold:
  1. model win probability >= min_prob (default 70%)
  2. win probability at the PESSIMISTIC end of the model's range still beats
     the break-even rate (price + fee): it wins often AND isn't overpriced
  3. central win probability beats break-even by at least min_margin
  4. confidence at least MEDIUM (unless allow_low)
Picks are ranked by the pessimistic win probability, then by margin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Analysis

CONF = ["NONE", "LOW", "MEDIUM", "HIGH"]


@dataclass
class Pick:
    analysis: Analysis
    side: str
    label: str
    win_p: float
    win_low: float
    win_high: float
    price: float              # average fill per contract
    fee: float                # per contract
    fillable: Optional[bool]

    @property
    def breakeven(self) -> float:
        return self.price + self.fee

    @property
    def margin(self) -> float:
        return self.win_p - self.breakeven

    @property
    def risk_to_win(self) -> str:
        win = 1 - self.breakeven
        return f"risk ${self.breakeven:.2f} to win ${win:.2f}" if win > 0 else "no profit possible"

    @property
    def losses_wiped(self) -> float:
        """How many wins one loss cancels out."""
        win = 1 - self.breakeven
        return self.breakeven / win if win > 0 else float("inf")


def describe(a: Analysis, side: str) -> str:
    sub = a.market.yes_sub_title or a.market.title
    if side == "YES":
        return f"YES: {sub}"
    m = sub.rstrip()
    # "Kevin Durant: 25+" -> "NO: Kevin Durant 25+ (24 or fewer)"
    import re
    x = re.search(r"(\d+)\+\s*$", m)
    return f"NO: {m}" + (f" (finishes with {int(x.group(1)) - 1} or fewer)" if x else "")


def high_prob_picks(results: list[Analysis], min_prob: float = 0.70, min_margin: float = 0.02,
                    allow_low: bool = False) -> list[Pick]:
    picks = []
    for a in results:
        e = a.estimate
        if e.insufficient or a.verdict == "INSUFFICIENT DATA":
            continue
        if not allow_low and CONF.index(e.confidence) < CONF.index("MEDIUM"):
            continue
        for side, cost in (("YES", a.yes_cost), ("NO", a.no_cost)):
            if not cost or cost.avg_fill is None:
                continue
            wp = e.p_yes if side == "YES" else 1 - e.p_yes
            lo = e.p_low if side == "YES" else 1 - e.p_high
            hi = e.p_high if side == "YES" else 1 - e.p_low
            pk = Pick(a, side, describe(a, side), wp, lo, hi, cost.avg_fill, cost.fee_per_contract, cost.fillable)
            if wp >= min_prob and lo > pk.breakeven and pk.margin >= min_margin and pk.fillable is not False:
                picks.append(pk)
    return sorted(picks, key=lambda p: (p.win_low, p.margin), reverse=True)


def text_picks(picks: list[Pick], min_prob: float) -> str:
    if not picks:
        return (f"HIGH-PROBABILITY PICKS: none. No side had a {min_prob:.0%}+ win probability that also beat "
                "its break-even price at the pessimistic end of the model's range.")
    hdr = f"{'#':<3} {'Pick':<46} {'Win %':>6} {'Range':>9} {'Price':>6} {'B/even':>7} {'Margin':>7} Conf"
    L = ["HIGH-PROBABILITY PICKS (most likely to win AND not overpriced)", hdr, "-" * len(hdr)]
    games = {}
    for i, p in enumerate(picks, 1):
        e = p.analysis.estimate
        L.append(f"{i:<3} {p.label[:46]:<46} {p.win_p:>6.0%} {p.win_low:>4.0%}-{p.win_high:<4.0%} "
                 f"{p.price * 100:>5.0f}c {p.breakeven:>7.1%} {p.margin * 100:>+6.1f}pp {e.confidence}")
        g = e.params.get("game")
        if g:
            games.setdefault(g, []).append(i)
    L.append("")
    L.append("Break-even = price + fee: the win rate needed just to not lose money.")
    worst = max(picks, key=lambda p: p.losses_wiped)
    L.append(f"At {worst.price * 100:.0f}c, one loss cancels about {worst.losses_wiped:.0f} wins. "
             "High win rates come with small payouts; keep each position small.")
    same = [v for v in games.values() if len(v) > 1]
    if same:
        L.append("Picks " + "; ".join(", ".join(f"#{i}" for i in v) for v in same) +
                 " are in the same game and tend to win or lose together.")
    return "\n".join(L)
