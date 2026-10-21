"""NBA game markets: winner (KXNBAGAME), margin (KXNBASPREAD), total (KXNBATOTAL).

Evidence: sportsbook lines with the bookmaker margin ("vig") removed.
  * Winner: de-vigged moneyline. The spread-implied win probability is used
    as a cross-check that widens the range when the two disagree.
  * Margin / total: home margin M ~ Normal(mu, SD_MARGIN), total T ~ Normal(mu, SD_TOTAL),
    with mu solved from the book's main line and its de-vigged price.

The two SDs are long-run NBA ballpark figures, NOT fitted to this season.
Tails (alt lines far from the main line) are where the normal shape is least
reliable, so confidence drops there. Calibration should tune both SDs.

This model does NOT know about injuries, rest or lineup news except through
the lines. If a line is older than the news, the estimate is stale.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from ..external.nba_odds import TEAMS, BookLine, GameOdds, NBAOddsProvider
from ..models import ContractSpec, Estimate, Evidence, utcnow
from .base import Estimator, norm_cdf

SD_MARGIN = 12.5   # points; final margin around the closing spread (ballpark, uncalibrated)
SD_TOTAL = 18.5    # points; final total around the closing total (ballpark, uncalibrated)
SHARP_BOOKS = {"pinnacle", "circa sports", "bookmaker", "betfair", "betonline.ag"}
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
NBA_SERIES = ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL")


# ------------------------------------------------------------------ maths
def american_to_prob(o: float) -> float:
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def devig(o1: float, o2: float, method: str = "proportional") -> float:
    """Fair probability of side 1 from a two-way price."""
    p1, p2 = american_to_prob(o1), american_to_prob(o2)
    if method == "proportional":
        return p1 / (p1 + p2)
    lo, hi = 1.0, 3.0                       # power method: find k with p1^k + p2^k = 1
    for _ in range(60):
        k = (lo + hi) / 2
        if p1 ** k + p2 ** k > 1:
            lo = k
        else:
            hi = k
    return p1 ** ((lo + hi) / 2)


def inv_norm(p: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def parse_nba_ticker(ticker: str) -> Optional[dict]:
    m = re.match(r"^(KXNBA\w+?)-(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})-([A-Z]{3})?(\d+)?$", ticker)
    if not m or m.group(3) not in MONTHS:
        return None
    return {"series": m.group(1),
            "date": date(2000 + int(m.group(2)), MONTHS[m.group(3)], int(m.group(4))),
            "away": m.group(5), "home": m.group(6), "team": m.group(7), "strike_code": m.group(8)}


def _threshold(x: float) -> float:
    """'Over X' on an integer score: X.5 stays; integer X means at least X+1."""
    return x if x != int(x) else x + 0.5


# ------------------------------------------------------------------ per-book probabilities
def _win_probs(b: BookLine, team_is_home: bool) -> tuple[list[float], Optional[float]]:
    """Moneyline-based win probabilities (both devig methods) and spread-implied one."""
    ml = []
    if b.ml_home is not None and b.ml_away is not None:
        for meth in ("proportional", "power"):
            ph = devig(b.ml_home, b.ml_away, meth)
            ml.append(ph if team_is_home else 1 - ph)
    mu = _margin_mu(b)
    sp = None
    if mu is not None:
        ph = norm_cdf(mu / SD_MARGIN)
        sp = ph if team_is_home else 1 - ph
    return ml, sp


def _margin_mu(b: BookLine, sd: float = SD_MARGIN) -> Optional[float]:
    """Expected home margin from the spread line and its de-vigged price."""
    if b.spread_home is None:
        if b.ml_home is not None and b.ml_away is not None:
            return sd * inv_norm(devig(b.ml_home, b.ml_away))
        return None
    cover = 0.5
    if b.spread_home_odds is not None and b.spread_away_odds is not None:
        cover = devig(b.spread_home_odds, b.spread_away_odds)
    return -b.spread_home + sd * inv_norm(cover)


def _total_mu(b: BookLine, sd: float = SD_TOTAL) -> Optional[float]:
    if b.total is None:
        return None
    p_over = 0.5
    if b.over_odds is not None and b.under_odds is not None:
        p_over = devig(b.over_odds, b.under_odds)
    return b.total + sd * inv_norm(p_over)


# ------------------------------------------------------------------ estimator
class NBAEstimator(Estimator):
    name = "nba_lines_v1"

    def __init__(self, provider: NBAOddsProvider):
        self.provider = provider

    def applies(self, spec: ContractSpec, evidence: dict) -> bool:
        return spec.series_ticker in NBA_SERIES

    def estimate(self, spec: ContractSpec, evidence: dict,
                 now: Optional[datetime] = None) -> Estimate:
        now = now or utcnow()
        t = parse_nba_ticker(spec.ticker)
        if not t or t["away"] not in TEAMS or t["home"] not in TEAMS:
            return Estimate.insufficient_data(self.name, "Could not read the teams/date from the ticker.")
        kind = {"KXNBAGAME": "winner", "KXNBASPREAD": "margin", "KXNBATOTAL": "total"}[t["series"]]
        if kind in ("winner", "margin") and t["team"] not in TEAMS:
            return Estimate.insufficient_data(self.name, "Could not tell which team this contract is on.")
        if kind in ("margin", "total") and spec.floor_strike is None:
            return Estimate.insufficient_data(self.name, "Contract has no strike value.")
        if spec.strike_type not in (None, "structured", "greater"):
            return Estimate.insufficient_data(self.name, f"Unsupported strike type '{spec.strike_type}'.")

        g = self.provider.game(t["away"], t["home"], t["date"])
        notes = list(self.provider.notes)
        if not g or not g.books:
            return Estimate.insufficient_data(
                self.name, f"No sportsbook lines found for {t['away']} @ {t['home']} on {t['date']}. "
                           + " ".join(notes))
        if g.commence_time and now >= g.commence_time:
            return Estimate.insufficient_data(
                self.name, "Game has tipped off; pre-game lines no longer describe it (Kalshi trades live).")

        team = t["team"]
        team_is_home = team == g.home
        cands, central, evs = [], [], []
        sharp = False
        x = spec.floor_strike
        for b in g.books:
            w = 3.0 if b.book.lower() in SHARP_BOOKS else 1.0
            sharp |= w > 1
            if kind == "winner":
                ml, sp = _win_probs(b, team_is_home)
                if not ml and sp is None:
                    continue
                if ml:
                    central.append((ml[0], w))
                    cands += ml
                if sp is not None:
                    cands.append(sp)
                    if not ml:
                        central.append((sp, w * 0.5))
                desc = (f"{b.book}: ML {b.ml_home:+.0f}/{b.ml_away:+.0f} (home/away)" if ml else f"{b.book}: no ML")
                if b.spread_home is not None:
                    desc += f", spread home {b.spread_home:+g}"
            elif kind == "margin":
                thr = _threshold(x)
                vals = []
                for k in (1.0, 0.85, 1.15):
                    mu = _margin_mu(b, SD_MARGIN * k)
                    if mu is None:
                        continue
                    sd = SD_MARGIN * k
                    vals.append(1 - norm_cdf((thr - mu) / sd) if team_is_home else norm_cdf((-thr - mu) / sd))
                if not vals:
                    continue
                central.append((vals[0], w))
                cands += vals
                desc = f"{b.book}: spread home {b.spread_home:+g}" if b.spread_home is not None else f"{b.book}: ML only"
            else:
                thr = _threshold(x)
                vals = []
                for k in (1.0, 0.85, 1.15):
                    mu = _total_mu(b, SD_TOTAL * k)
                    if mu is None:
                        continue
                    vals.append(1 - norm_cdf((thr - mu) / (SD_TOTAL * k)))
                if not vals:
                    continue
                central.append((vals[0], w))
                cands += vals
                desc = f"{b.book}: total {b.total:g} ({b.over_odds:+.0f}/{b.under_odds:+.0f})" \
                    if b.over_odds is not None else f"{b.book}: total {b.total}"
            evs.append(Evidence(b.source + f" — {b.book}", desc, central[-1][0], b.updated_at, b.url, b.is_mock))

        if not central:
            return Estimate.insufficient_data(self.name, f"Lines found, but none for this market type ({kind}).", evs)

        p = sum(v * w for v, w in central) / sum(w for _, w in central)
        ages = [(now - b.updated_at).total_seconds() / 3600 for b in g.books if b.updated_at]
        age_h = min(ages) if ages else None
        pad = 0.015 + (min(0.05, 0.01 * (age_h / 6)) if age_h is not None else 0.05)
        p_lo, p_hi = max(0.001, min(cands) - pad), min(0.999, max(cands) + pad)
        n_books = len(central)

        conf = "MEDIUM"
        why_down = []
        if n_books >= 3 and sharp and age_h is not None and age_h <= 2:
            conf = "HIGH"
        if age_h is None or age_h > 6:
            conf = "LOW"
            why_down.append("lines are more than 6 hours old" if age_h else "line time unknown")
        if kind == "margin":
            main = -next((b.spread_home for b in g.books if b.spread_home is not None), 0) * (1 if team_is_home else -1)
            if abs(x - main) > 10:
                conf = "LOW" if conf != "HIGH" else "MEDIUM"
                why_down.append(f"strike {x:g} is {abs(x - main):.0f} pts from the main line (tail of the distribution)")
        if kind == "total":
            main = next((b.total for b in g.books if b.total is not None), x)
            if abs(x - main) > 15:
                conf = "LOW" if conf != "HIGH" else "MEDIUM"
                why_down.append(f"strike {x:g} is {abs(x - main):.0f} pts from the main total (tail)")
        if any(e.is_mock for e in evs):
            conf = "LOW"
            why_down.append("MOCK lines")

        tname = TEAMS[team][1] if team else ""
        supporting = [f"{n_books} sportsbook line(s){' incl. a sharp book' if sharp else ''}; "
                      f"de-vigged estimate {p:.1%}."]
        if kind == "winner":
            sp_vals = [c for c in cands]
            supporting.append(f"Moneyline and spread-implied probabilities span {min(sp_vals):.0%}-{max(sp_vals):.0%}.")
        opposing = []
        if n_books == 1:
            opposing.append("Only one sportsbook. A single book can be slow or shaded; a sharp consensus "
                            "is much better evidence (set ODDS_API_KEY).")
        if age_h is not None and age_h > 1:
            opposing.append(f"Newest line is ~{age_h:.0f}h old; NBA lines move on injury and rest news.")
        if g.commence_time:
            hrs = (g.commence_time - now).total_seconds() / 3600
            if hrs > 12:
                opposing.append(f"Tip-off is {hrs:.0f}h away; lineups and injury statuses are not final.")
        opposing += [f"Data note: {n}" for n in notes]

        assumptions = ["Sportsbook prices, with the vig removed, are a good estimate of the true probability.",
                       "Vig removed two ways (proportional and power); both feed the range."]
        if kind == "margin":
            assumptions.append(f"Final margin ~ Normal around the spread, SD {SD_MARGIN} pts (uncalibrated default).")
        if kind == "total":
            assumptions.append(f"Final total ~ Normal around the book total, SD {SD_TOTAL} pts (uncalibrated default).")
            assumptions.append("Overtime points count, as in sportsbook totals. Check the Kalshi rules to confirm.")
        if why_down:
            assumptions.append("Confidence lowered: " + "; ".join(why_down) + ".")

        return Estimate(
            self.name, p, p_lo, p_hi, conf, evs, supporting, opposing, assumptions,
            would_change=["Injury report updates and late scratches (often 30-90 min before tip).",
                          "A line move at sharp books.", "Rest decisions on back-to-backs."],
            time_sensitive=True, created_at=now,
            params={"kind": kind, "team": team, "home": g.home, "away": g.away,
                    "tip": g.commence_time.isoformat() if g.commence_time else None})


# ------------------------------------------------------------------ parlay legs
def nba_leg_resolver(provider: NBAOddsProvider):
    """Resolve a parlay leg given as a Kalshi market ticker (from mve_selected_legs)
    when it is an NBA winner / margin / total market. Player props return None."""
    est = NBAEstimator(provider)

    def resolve(leg_ticker: str, side: str, now: datetime):
        t = parse_nba_ticker(leg_ticker)
        if not t or t["series"] not in NBA_SERIES:
            return None
        strike = int(t["strike_code"]) - 0.5 if t["strike_code"] else None
        spec = ContractSpec(leg_ticker, leg_ticker.rsplit("-", 1)[0], t["series"], "", "", "", "",
                            None, "greater" if strike is not None else None, strike, None)
        e = est.estimate(spec, {}, now)
        if e.insufficient:
            return None
        p, lo, hi = e.p_yes, e.p_low, e.p_high
        if side == "no":
            p, lo, hi = 1 - p, 1 - hi, 1 - lo
        game = f"{t['date']}:{t['away']}@{t['home']}"
        kind = e.params["kind"]
        cond = {"kind": kind, "team": t["team"], "side": side, "p": p, "lo": lo, "hi": hi,
                "thr": 0.0 if kind == "winner" else strike}
        return (p, lo, hi, f"NBA lines ({', '.join(x.source.split(' — ')[-1] for x in e.evidence)})",
                game, any(x.is_mock for x in e.evidence), cond)
    return resolve


def nba_same_game_joint(conds: list[dict]):
    """Joint probability for several YES legs on ONE game, using the fact that
    winner and margin legs are all statements about the same final margin:
      'SAS wins' AND 'SAS wins by over 7.5'  ==  'SAS wins by over 7.5'.
    Same for 'over' totals. Margin and total are treated as independent.
    Returns (p, lo, hi, note) or None when the combination isn't handled."""
    if any(c is None or c["side"] != "yes" or c.get("kind") not in ("winner", "margin", "total") for c in conds):
        return None
    margin = [c for c in conds if c["kind"] in ("winner", "margin")]
    total = [c for c in conds if c["kind"] == "total"]
    if len({c["team"] for c in margin}) > 1:
        return 0.0, 0.0, 0.0, "Legs require both teams to win: impossible."
    p = lo = hi = 1.0
    parts = []
    for chain, label in ((margin, "margin"), (total, "total")):
        if chain:
            top = max(chain, key=lambda c: c["thr"])
            p, lo, hi = p * top["p"], lo * top["lo"], hi * top["hi"]
            parts.append(f"{label} legs collapse to the strictest one (over {top['thr']:g})")
    return p, lo, hi, "; ".join(parts) + ("; margin and total assumed independent" if margin and total else "")
