"""Calibration of logged predictions against settled outcomes.

Reports, for the model AND for the market price at the same moment:
  * Brier score and log loss (lower is better). If the model does not beat the
    market's Brier score, its "edges" are probably noise.
  * Reliability table: within each probability bin, how often YES happened.
  * Bias toward YES/NO: mean prediction minus observed frequency.
  * Over/under-confidence: logistic calibration slope (1.0 = calibrated,
    <1 = overconfident, >1 = underconfident).
  * Hypothetical results of the calls flagged as edges (after logged fees).
"""
from __future__ import annotations

import math
from collections import defaultdict


def _clip(p):
    return min(max(p, 1e-4), 1 - 1e-4)


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def log_loss(ps, ys):
    return -sum(y * math.log(_clip(p)) + (1 - y) * math.log(1 - _clip(p)) for p, y in zip(ps, ys)) / len(ps)


def calibration_slope(ps, ys, iters: int = 50):
    """Fit y ~ sigmoid(a + b*logit(p)) by Newton's method. Returns (a, b)."""
    xs = [math.log(_clip(p) / (1 - _clip(p))) for p in ps]
    a, b = 0.0, 1.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            q = 1 / (1 + math.exp(-(a + b * x)))
            w = q * (1 - q)
            ga += y - q
            gb += (y - q) * x
            haa += w
            hab += w * x
            hbb += w * x * x
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a + da, b + db
        if abs(da) + abs(db) < 1e-8:
            break
    return a, b


def reliability(ps, ys, bins: int = 10):
    buckets = defaultdict(list)
    for p, y in zip(ps, ys):
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    rows = []
    for k in sorted(buckets):
        pts = buckets[k]
        rows.append({"bin": f"{k*100//bins}-{(k+1)*100//bins}%", "n": len(pts),
                     "mean_pred": sum(p for p, _ in pts) / len(pts),
                     "observed": sum(y for _, y in pts) / len(pts)})
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "message": "No resolved predictions yet."}
    ps = [r["model_prob"] for r in rows]
    ys = [r["outcome"] for r in rows]
    n = len(rows)
    out = {"n": n, "brier_model": brier(ps, ys), "log_loss_model": log_loss(ps, ys),
           "mean_pred": sum(ps) / n, "observed_rate": sum(ys) / n,
           "reliability": reliability(ps, ys)}
    paired = [(r["model_prob"], r["market_prob"], r["outcome"]) for r in rows if r["market_prob"] is not None]
    if paired:
        out["n_paired"] = len(paired)
        out["brier_model_paired"] = brier([a for a, _, _ in paired], [y for *_, y in paired])
        out["brier_market_paired"] = brier([b for _, b, _ in paired], [y for *_, y in paired])
    bias = out["mean_pred"] - out["observed_rate"]
    se = math.sqrt(max(out["observed_rate"] * (1 - out["observed_rate"]), 0.01) / n)
    out["yes_bias"] = bias
    out["yes_bias_label"] = ("biased toward YES" if bias > 2 * se else
                             "biased toward NO" if bias < -2 * se else "no clear YES/NO bias")
    if n >= 20 and len(set(ys)) == 2:
        _, b = calibration_slope(ps, ys)
        out["slope"] = b
        out["slope_label"] = ("overconfident" if b < 0.8 else "underconfident" if b > 1.25
                              else "roughly calibrated")
    else:
        out["slope_label"] = "need 20+ resolved predictions with both outcomes"
    # Flagged edges: hypothetical P&L buying the best side at the logged fill.
    flagged = [r for r in rows if r["verdict"] in ("POTENTIAL EDGE", "STRONG POTENTIAL EDGE")
               and r["best_fill"] is not None]
    if flagged:
        pnl = 0.0
        for r in flagged:
            won = r["outcome"] == (1 if r["best_side"] == "YES" else 0)
            pnl += (1.0 if won else 0.0) - r["best_fill"] - (r["best_fee"] or 0)
        out["flagged_n"] = len(flagged)
        out["flagged_pnl_per_contract"] = pnl / len(flagged)
    out["warning"] = ("Fewer than 50 resolved predictions: treat every number here as preliminary."
                      if n < 50 else None)
    return out


def by_group(rows: list[dict], key: str) -> dict[str, dict]:
    groups = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    return {k: summarize(v) for k, v in groups.items()}


def format_report(rows: list[dict]) -> str:
    s = summarize(rows)
    if s["n"] == 0:
        return s["message"]
    L = [f"CALIBRATION  (n = {s['n']} resolved markets)"]
    if s.get("warning"):
        L.append(f"  ! {s['warning']}")
    L.append(f"  Brier (model): {s['brier_model']:.4f}   log loss: {s['log_loss_model']:.4f}")
    if "brier_market_paired" in s:
        better = "model" if s["brier_model_paired"] < s["brier_market_paired"] else "market"
        L.append(f"  Same markets, n={s['n_paired']}: model {s['brier_model_paired']:.4f} vs "
                 f"market {s['brier_market_paired']:.4f}  -> {better} was more accurate")
    L.append(f"  Mean forecast {s['mean_pred']:.1%} vs observed {s['observed_rate']:.1%}: {s['yes_bias_label']}")
    slope = f"slope {s['slope']:.2f}, " if "slope" in s else ""
    L.append(f"  Confidence: {slope}{s['slope_label']}")
    if "flagged_n" in s:
        L.append(f"  Flagged edges: {s['flagged_n']}, hypothetical result "
                 f"{s['flagged_pnl_per_contract']*100:+.1f}c per contract after logged fees")
    L.append("  Reliability (predicted vs observed):")
    for r in s["reliability"]:
        L.append(f"    {r['bin']:>8}  n={r['n']:<4} predicted {r['mean_pred']:.0%}  observed {r['observed']:.0%}")
    for key in ("estimator", "confidence"):
        g = by_group(rows, key)
        if len(g) > 1:
            L.append(f"  By {key}:")
            for k, v in g.items():
                L.append(f"    {k:<20} n={v['n']:<4} Brier {v['brier_model']:.4f}  {v['yes_bias_label']}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Patterns in high-probability calls (props)
# ---------------------------------------------------------------------------
import re as _re

_PROP = _re.compile(r"^KXNBA(PTS|REB|AST)-\d{2}[A-Z]{3}\d{2}([A-Z]{3})([A-Z]{3})-[A-Z]{3}\D+\d+-(\d+)$")
_STAT = {"PTS": "Points", "REB": "Rebounds", "AST": "Assists"}


def _sides(rows: list[dict], min_p: float):
    """Each settled prop contributes the side the model rated >= min_p, if any."""
    out = []
    for r in rows:
        m = _PROP.match(r.get("ticker") or "")
        if not m or r.get("model_prob") is None or r.get("outcome") is None:
            continue
        for side, p in (("YES", r["model_prob"]), ("NO", 1 - r["model_prob"])):
            if p < min_p:
                continue
            ask = r.get("yes_ask") if side == "YES" else (1 - r["yes_bid"] if r.get("yes_bid") is not None else None)
            if ask is not None and not 0 < ask < 1:
                ask = None
            mkt = r.get("market_prob")
            if mkt is not None and side == "NO":
                mkt = 1 - mkt
            out.append({"stat": _STAT[m.group(1)], "side": side, "p": p, "k": int(m.group(4)),
                        "won": r["outcome"] == (1 if side == "YES" else 0), "ask": ask, "mkt": mkt,
                        "game": r.get("event_ticker", "").split("-")[-1]})
    return out


def _group(sides, key):
    g: dict = {}
    for s in sides:
        g.setdefault(key(s), []).append(s)
    rows = []
    for k, v in g.items():
        priced = [x for x in v if x["ask"] is not None]
        pnl = (sum((1.0 if x["won"] else 0.0) - x["ask"] - 0.07 * x["ask"] * (1 - x["ask"]) for x in priced)
               / len(priced)) if priced else None
        mk = [x["mkt"] for x in v if x["mkt"] is not None]
        rows.append({"group": k, "n": len(v), "games": len({x["game"] for x in v}),
                     "hit": sum(x["won"] for x in v) / len(v), "model": sum(x["p"] for x in v) / len(v),
                     "kalshi": sum(mk) / len(mk) if mk else None, "pnl": pnl})
    return sorted(rows, key=lambda r: -r["n"])


def pattern_tables(rows: list[dict], min_p: float = 0.82) -> list[tuple[str, list[dict]]]:
    sides = _sides(rows, min_p)
    if not sides:
        return []
    price = lambda s: "n/a" if s["ask"] is None else "under 80¢" if s["ask"] < 0.8 else \
        "80–90¢" if s["ask"] < 0.9 else "90¢+"
    rung = lambda s: "Low rung (2+, 4+)" if s["k"] <= 4 else "Higher rung (5+)"
    return [("Stat and side", _group(sides, lambda s: f"{s['stat']} {s['side']}")),
            ("Low vs higher rungs", _group(sides, lambda s: f"{rung(s)} {s['side']}")),
            ("Price paid", _group(sides, price))]


def _cents(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}¢"


def pattern_html(rows: list[dict], min_p: float = 0.82) -> str:
    tables = pattern_tables(rows, min_p)
    if not tables:
        return ""
    n_games = len({s["game"] for s in _sides(rows, min_p)})

    def pct(x):
        return "n/a" if x is None else f"{x:.0%}"

    parts = [f"<h2>What's hitting</h2><p class='note'>Every settled prop the model rated {min_p:.0%}+ on either "
             f"side, across {n_games} games. <b>Hit</b> is how often it won; <b>Kalshi</b> is the price-implied "
             "chance when the model ran; <b>Result</b> is profit per contract if you had bought all of them at "
             "Kalshi's ask, after fees. Props in one game win or lose together, so small groups and few games "
             "are mostly noise.</p>"]
    for title, grp in tables:
        body = "".join(
            f"<tr><td>{g['group']}</td><td class='num'>{g['n']}</td><td class='num'>{g['games']}</td>"
            f"<td class='num'>{pct(g['hit'])}</td><td class='num'>{pct(g['model'])}</td>"
            f"<td class='num'>{pct(g['kalshi'])}</td>"
            f"<td class='num'>{_cents(g['pnl'])}</td></tr>"
            for g in grp)
        parts.append(f"<h3>{title}</h3><div class='tbl'><table><thead><tr><th>Group</th><th>Props</th>"
                     "<th>Games</th><th>Hit</th><th>Model said</th><th>Kalshi</th><th>Result</th></tr></thead>"
                     f"<tbody>{body}</tbody></table></div>")
    return "<section class='sheet'>" + "".join(parts) + "</section>"
