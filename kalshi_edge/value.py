"""Value watchlist: cheap contracts the model thinks are underpriced.

These are PRACTICE picks. They appear on the report and are scored afterwards,
but are never sent as alerts. So far, when the model disagrees strongly with
Kalshi, Kalshi has usually been closer to right; this list exists to test
whether cheap, model-favoured contracts make money before anyone bets on them.

A side qualifies when (settings in picks_settings.toml):
  * price (ask) <= value_max_price_cents
  * model win chance - (price + fee) >= value_min_edge_points
  * Kalshi bid-ask gap <= value_max_spread_cents
The sportsbook check, when lines were available, is shown as agrees / disagrees.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .models import Analysis
from .user_settings import load as load_settings

FEE_RATE = 0.07
_PROP = re.compile(r"^KXNBA(PTS|REB|AST)-")


def _fee(price: float) -> float:
    return FEE_RATE * price * (1 - price)


def _book_view(p_book: Optional[float], side: str, price: float) -> str:
    if p_book is None:
        return "no line"
    pb = p_book if side == "YES" else 1 - p_book
    return "agrees" if pb - price - _fee(price) >= 0.05 else "disagrees"


@dataclass
class ValuePick:
    analysis: Analysis
    side: str
    label: str
    win_p: float
    price: float
    edge: float               # win_p - price - fee
    book: str                 # agrees / disagrees / no line


def _rules():
    c = load_settings()
    return c["value_max_price_cents"] / 100, c["value_min_edge_points"] / 100, c["value_max_spread_cents"] / 100


def value_picks(results: list[Analysis]) -> list[ValuePick]:
    from .highprob import describe
    max_price, min_edge, max_spread = _rules()
    out = []
    for a in results:
        e = a.estimate
        if e.insufficient or not _PROP.match(a.market.ticker):
            continue
        if a.market.spread is None or a.market.spread > max_spread + 1e-9:
            continue
        for side, cost in (("YES", a.yes_cost), ("NO", a.no_cost)):
            if not cost or cost.top_of_book is None:
                continue
            price = cost.top_of_book
            wp = e.p_yes if side == "YES" else 1 - e.p_yes
            edge = wp - price - _fee(price)
            if price <= max_price + 1e-9 and edge >= min_edge:
                out.append(ValuePick(a, side, describe(a, side), wp, price, edge,
                                     _book_view(e.params.get("p_book"), side, price)))
    order = {"agrees": 0, "no line": 1, "disagrees": 2}
    return sorted(out, key=lambda v: (order[v.book], -v.edge))


def value_results(rows: list[dict]) -> dict:
    """Apply today's rules to every settled, logged prop and score them."""
    max_price, min_edge, max_spread = _rules()
    groups: dict = {}
    for r in rows:
        if not _PROP.match(r.get("ticker") or "") or r.get("model_prob") is None or r.get("outcome") is None:
            continue
        if r.get("spread") is None or r["spread"] > max_spread + 1e-9:
            continue
        try:
            p_book = json.loads(r.get("evidence_json") or "{}").get("params", {}).get("p_book")
        except ValueError:
            p_book = None
        for side, wp in (("YES", r["model_prob"]), ("NO", 1 - r["model_prob"])):
            price = r.get("yes_ask") if side == "YES" else (1 - r["yes_bid"] if r.get("yes_bid") is not None else None)
            if price is None or not 0 < price < 1:
                continue
            if price > max_price + 1e-9 or wp - price - _fee(price) < min_edge:
                continue
            won = r["outcome"] == (1 if side == "YES" else 0)
            g = groups.setdefault(_book_view(p_book, side, price), [])
            g.append((won, price, (1.0 if won else 0.0) - price - _fee(price), wp,
                      (r.get("event_ticker") or "").split("-")[-1]))
    out = {}
    allrows = [x for v in groups.values() for x in v]
    for name, v in [("All watchlist picks", allrows)] + [(f"Sportsbook {k}", groups[k])
                                                        for k in ("agrees", "no line", "disagrees") if k in groups]:
        if not v:
            continue
        n = len(v)
        out[name] = {"n": n, "games": len({x[4] for x in v}), "hit": sum(x[0] for x in v) / n,
                     "model": sum(x[3] for x in v) / n, "paid": sum(x[1] for x in v) / n,
                     "result": sum(x[2] for x in v) / n}
    return out


def value_html(picks: list[ValuePick], rows: list[dict]) -> str:
    max_price, min_edge, max_spread = _rules()
    rules = (f"under {max_price * 100:.0f}¢, the model's win chance beats price + fee by {min_edge * 100:.0f}+ points, "
             f"and Kalshi's bid–ask gap is {max_spread * 100:.0f}¢ or less")
    parts = ["<section class='sheet'><h2>Value watchlist (practice only)</h2>"
             f"<p class='note'>Cheap contracts the model thinks are underpriced: {rules}. These are tracked to test "
             "the strategy and are <b>not</b> sent as alerts. So far, when the model and Kalshi disagree a lot, "
             "Kalshi has usually been closer to right, so treat this list as an experiment until the results "
             "below say otherwise.</p>"]
    if picks:
        body = "".join(
            f"<tr><td><a href='#{v.analysis.market.ticker}'>{v.label}</a></td>"
            f"<td class='num'>{v.price * 100:.0f}¢</td><td class='num'>{v.win_p:.0%}</td>"
            f"<td class='num'>{v.edge * 100:+.0f}</td><td>{v.book}</td>"
            f"<td>{v.analysis.estimate.confidence}</td></tr>" for v in picks[:25])
        parts.append("<div class='tbl'><table><thead><tr><th>Pick</th><th>Price</th><th>Model</th>"
                     "<th>Edge (pts)</th><th>Sportsbooks</th><th>Confidence</th></tr></thead>"
                     f"<tbody>{body}</tbody></table></div>")
        if len(picks) > 25:
            parts.append(f"<p class='note'>{len(picks) - 25} more not shown.</p>")
    else:
        parts.append("<p class='note'>No contracts met the watchlist rules today.</p>")
    res = value_results(rows)
    if res:
        body = "".join(
            f"<tr><td>{k}</td><td class='num'>{v['n']}</td><td class='num'>{v['games']}</td>"
            f"<td class='num'>{v['model']:.0%}</td><td class='num'>{v['paid'] * 100:.0f}¢</td>"
            f"<td class='num'>{v['hit']:.0%}</td><td class='num'>{v['result'] * 100:+.1f}¢</td></tr>"
            for k, v in res.items())
        parts.append("<h3>Watchlist results so far</h3><p class='note'>Today's rules applied to every settled "
                     "prop in the log. <b>Result</b> is profit per contract at Kalshi's ask, after fees. "
                     "If the model were right, <b>Hit</b> would be close to <b>Model</b>; if Kalshi were right, "
                     "close to <b>Avg paid</b>.</p><div class='tbl'><table><thead><tr><th>Group</th>"
                     "<th>Picks</th><th>Games</th><th>Model</th><th>Avg paid</th><th>Hit</th><th>Result</th>"
                     f"</tr></thead><tbody>{body}</tbody></table></div>")
    return "".join(parts) + "</section>"
