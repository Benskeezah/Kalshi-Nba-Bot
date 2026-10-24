"""NBA player props: Kalshi KXNBAPTS / KXNBAREB / KXNBAAST ladders ("Kevin Durant: 25+ points").

Model: the stat X is a count, modelled as Negative Binomial(mean mu, size r).
  * mu comes from sportsbook prop lines with the vig removed: for each line we
    solve for the mu that makes P(X > line) equal the fair over-probability.
  * r (spread of outcomes) comes from the player's game log when there are
    enough games, otherwise a default per stat. Bigger r = tighter.
  * Game logs alone give a second, weaker estimate of mu. Logs don't know
    tonight's injuries, matchup, minutes plan or role changes.

Kalshi rules: a player who is inactive or never enters the game settles at a
"fair price", so this model is conditional on the player playing. Once he
enters, early exits count, which is why r includes minutes risk.
"""
from __future__ import annotations

import math
import os
import re
import statistics
from datetime import datetime, timezone
from typing import Optional

from ..external.nba_odds import TEAMS
from ..external.nba_props import STATS, PropProvider
from ..external.nba_stats import StatsDB, fetch_injuries
from .nba_projection import project
from ..user_settings import load as load_settings
from ..models import ContractSpec, Estimate, Evidence, utcnow
from .base import Estimator
from ..external.nba_odds import NBAOddsProvider
from .nba import MONTHS, american_to_prob, devig

PROP_SERIES = {"KXNBAPTS": "points", "KXNBAREB": "rebounds", "KXNBAAST": "assists"}
# Default NB size per stat (uncalibrated). Points vary more relative to their mean.
DEFAULT_R = {"points": 14.0, "rebounds": 25.0, "assists": 30.0}
ONE_SIDED_OVERROUND = (0.03, 0.09)    # assumed book margin when only one side is quoted
SHARP_BOOKS = {"pinnacle", "circa sports", "bookmaker", "betonline.ag"}


# ------------------------------------------------------------------ distribution
def nb_sf(k: int, mu: float, r: float) -> float:
    """P(X >= k) for Negative Binomial with mean mu, size r."""
    if k <= 0:
        return 1.0
    mu = max(mu, 1e-6)
    p = r / (r + mu)
    pmf = math.exp(r * math.log(p))
    cdf = pmf
    for x in range(0, k - 1):
        pmf *= (x + r) / (x + 1) * (1 - p)
        cdf += pmf
    return max(0.0, min(1.0, 1 - cdf))


SHORT_NIGHT_SHARE = 0.35   # a short night (injury exit, foul trouble, benched) ~ 35% of normal output


def short_night_chance(spread_abs: Optional[float], b2b: bool = False) -> float:
    """Chance this player has a short-minutes night. Set in picks_settings.toml;
    higher for likely blowouts (starters sit) and back-to-backs."""
    q = load_settings()["short_night_chance_percent"] / 100
    if spread_abs is not None and spread_abs >= 10:
        q += 0.02
    if b2b:
        q += 0.01
    return min(q, 0.3)


def p_at_least(k: int, mu: float, r: float, q: float) -> float:
    """P(X >= k) allowing a q chance of a short night. This mainly lowers
    near-certain low rungs ("2+ assists"), which a plain count model makes too sure."""
    return (1 - q) * nb_sf(k, mu, r) + q * nb_sf(k, mu * SHORT_NIGHT_SHARE, r)


def p_over_line(line: float, mu: float, r: float, q: float = 0.0) -> float:
    """Fair P(over) for a sportsbook line. Integer lines push on equality."""
    if line != int(line):
        return p_at_least(int(math.floor(line)) + 1, mu, r, q)
    above, at_or_above = p_at_least(int(line) + 1, mu, r, q), p_at_least(int(line), mu, r, q)
    push = at_or_above - above
    return above / (1 - push) if push < 1 else 0.5


def solve_mu(line: float, q_over: float, r: float, q: float = 0.0) -> float:
    lo, hi = 0.01, 120.0
    for _ in range(70):
        mid = (lo + hi) / 2
        if p_over_line(line, mid, r, q) < q_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def fair_over_probs(l) -> list[float]:
    """All plausible fair over-probabilities for one quoted line."""
    if l.over is not None and l.under is not None:
        return [devig(l.over, l.under, "proportional"), devig(l.over, l.under, "power")]
    if l.over is not None:
        po = american_to_prob(l.over)
        return [po / (1 + m) for m in ONE_SIDED_OVERROUND]
    if l.under is not None:
        pu = american_to_prob(l.under)
        return [1 - pu / (1 + m) for m in ONE_SIDED_OVERROUND]
    return []


def parse_prop_ticker(ticker: str) -> Optional[dict]:
    m = re.match(r"^(KXNBA(?:PTS|REB|AST))-(\d{2})([A-Z]{3})(\d{2})([A-Z]{3})([A-Z]{3})-([A-Z]{3})([A-Z]+?)(\d+)-(\d+)$",
                 ticker)
    if not m or m.group(3) not in MONTHS:
        return None
    from datetime import date
    return {"series": m.group(1), "stat": PROP_SERIES[m.group(1)],
            "date": date(2000 + int(m.group(2)), MONTHS[m.group(3)], int(m.group(4))),
            "away": m.group(5), "home": m.group(6), "team": m.group(7),
            "name_code": m.group(8), "jersey": m.group(9), "k": int(m.group(10))}


def player_from_title(title: str) -> Optional[str]:
    return title.split(":")[0].strip() if ":" in (title or "") else None


def weighted_log_mean(vals: list[float], half_life: float = 15.0, n: int = 40) -> float:
    vals = vals[:n]
    w = [0.5 ** (i / half_life) for i in range(len(vals))]
    return sum(v * x for v, x in zip(vals, w)) / sum(w)


def r_from_log(vals: list[float], default: float) -> float:
    if len(vals) < 15:
        return default
    m, v = statistics.fmean(vals[:60]), statistics.pvariance(vals[:60])
    if v <= m:
        return 200.0                      # no over-dispersion: effectively Poisson
    return max(3.0, min(200.0, m * m / (v - m)))


# ------------------------------------------------------------------ estimator
class NBAPropEstimator(Estimator):
    name = "nba_props_v1"

    def __init__(self, props: PropProvider, games: NBAOddsProvider, stats: Optional[StatsDB] = None,
                 line_weight: Optional[float] = None, use_live: bool = True, stats_mock: bool = False):
        self.props, self.games, self.stats = props, games, stats
        self.stats_mock = stats_mock
        # How much sportsbook lines count vs the stats model when both exist (0 = stats only).
        if line_weight is None:
            env = os.environ.get("PROP_LINE_WEIGHT")
            line_weight = float(env) if env else load_settings()["sportsbook_weight"]
        self.line_weight = line_weight
        self.use_live = use_live
        self._injuries = None

    def injuries(self) -> dict:
        if self._injuries is None:
            self._injuries = {}
            if self.use_live:
                try:
                    self._injuries = fetch_injuries()
                except Exception:
                    self.props.notes.append("ESPN injury report unavailable.")
        return self._injuries

    def applies(self, spec: ContractSpec, evidence: dict) -> bool:
        return spec.series_ticker in PROP_SERIES

    def estimate(self, spec: ContractSpec, evidence: dict, now: Optional[datetime] = None) -> Estimate:
        now = now or utcnow()
        t = parse_prop_ticker(spec.ticker)
        player = player_from_title(spec.title) or evidence.get("player")
        if not t or not player or t["team"] not in TEAMS:
            return Estimate.insufficient_data(self.name, "Could not read the player, stat or threshold from the market.")
        stat, k, team = t["stat"], t["k"], t["team"]
        opp = t["home"] if team == t["away"] else t["away"]

        g = self.games.game(t["away"], t["home"], t["date"])
        tip = g.commence_time if g else None
        if tip and now >= tip:
            return Estimate.insufficient_data(self.name, "Game has tipped off; pre-game numbers no longer apply.")
        tip_est = tip or datetime(t["date"].year, t["date"].month, t["date"].day, 23, tzinfo=timezone.utc)
        spread_abs = next((abs(b.spread_home) for b in (g.books if g else []) if b.spread_home is not None), None)

        lines = self.props.lines(player, stat, t["home"], t["away"], tip)
        notes = list(self.props.notes)
        evs: list[Evidence] = []
        mock = bool(evidence.get("_mock")) or bool(self.stats_mock)

        # ---- 1. stats projection (matchup, averages, minutes, pace, rest)
        proj = None
        if self.stats:
            proj = project(self.stats, player, stat, team, opp, team == t["home"], tip_est, spread_abs,
                           self.injuries())
        log = None
        if not proj:
            season = t["date"].year + 1 if t["date"].month >= 8 else t["date"].year
            log = self.props.log(player, stat, team, season)
        values = proj.values if proj else (log.values if log else [])
        r = r_from_log(values, DEFAULT_R[stat])
        r_src = f"his game log ({len(values)} games)" if len(values) >= 15 else "default"

        b2b = bool(proj and any("back-to-back" in f for f in proj.factors))
        q = short_night_chance(spread_abs, b2b)

        # ---- 2. sportsbook lines (cross-check / blend)
        mu_points, line_cands = [], []
        newest = max((l.updated_at for l in lines if l.updated_at), default=None)
        for l in lines:
            qs = fair_over_probs(l)
            if not qs:
                continue
            two = l.over is not None and l.under is not None
            w = (3.0 if l.book.lower() in SHARP_BOOKS else 1.0) * (2.0 if two else 1.0)
            if newest and l.updated_at:      # older quotes count less (half-life 4 hours)
                w *= 0.5 ** ((newest - l.updated_at).total_seconds() / 3600 / 4)
            mu_points.append((solve_mu(l.line, statistics.fmean(qs), r, q), w))
            line_cands += [solve_mu(l.line, qo, rr, q) for qo in qs for rr in (r, r * 0.6, r * 1.6)]
            price = (f"over {l.over:+.0f} / under {l.under:+.0f}" if two else
                     f"over {l.over:+.0f} only" if l.over is not None else f"under {l.under:+.0f} only")
            evs.append(Evidence(f"{l.source} — {l.book}", f"{player} {stat} {l.line:g}: {price}",
                                l.line, l.updated_at, l.url, l.is_mock))
        mu_lines = sum(m * w for m, w in mu_points) / sum(w for _, w in mu_points) if mu_points else None

        mu_stats = proj.mu if proj else (weighted_log_mean(log.values) if log and len(log.values) >= 5 else None)
        if proj:
            evs.append(Evidence("Stats projection (ESPN box scores)",
                                f"{player}: projected {proj.mu:.1f} {stat} from {proj.n_games} logged games",
                                proj.mu, None, None, mock))
        elif mu_stats is not None:
            evs.append(Evidence(log.source, f"{player} {stat}: recent weighted average {mu_stats:.1f} "
                                f"({log.season_label}); no matchup data", mu_stats, None, log.url, log.is_mock))

        if mu_stats is None and mu_lines is None:
            return Estimate.insufficient_data(
                self.name, f"No stats history and no sportsbook {stat} line for {player}. Run "
                           "'build-stats' and/or set ODDS_API_KEY. " + " ".join(notes), evs)

        # ---- 3. combine
        w_line = self.line_weight
        if mu_stats is not None and mu_lines is not None:
            mu = (1 - w_line) * mu_stats + w_line * mu_lines
            basis = f"blend: {1 - w_line:.0%} stats model, {w_line:.0%} sportsbook lines"
        elif mu_stats is not None:
            mu, basis = mu_stats, "stats model only (no sportsbook line)"
        else:
            mu, basis = mu_lines, "sportsbook lines only (no stats history)"
        mu_cands = [mu] + line_cands
        if proj:
            mu_cands += [proj.mu_low, proj.mu_high]
        elif mu_stats is not None:
            mu_cands += [mu_stats * 0.85, mu_stats * 1.15]

        p = p_at_least(k, mu, r, q)
        grid = [p_at_least(k, m, rr, qq) for m in mu_cands for rr in (r, r * 0.6, r * 1.6)
                for qq in (q * 0.5, q, q * 1.6)] + [p]
        line_age = [(now - l.updated_at).total_seconds() / 3600 for l in lines if l.updated_at]
        age_h = min(line_age) if line_age else None
        pad = 0.015 + (min(0.05, 0.01 * age_h / 3) if age_h is not None else 0.02)
        p_lo, p_hi = max(0.001, min(grid) - pad), min(0.999, max(grid) + pad)

        # ---- 4. confidence
        two_sided = [l for l in lines if l.over is not None and l.under is not None]
        books = {l.book for l in lines}
        why = []
        if mu_lines is not None and len(books) >= 3 and two_sided and age_h is not None and age_h <= 2 and proj:
            conf = "HIGH"
        elif (mu_lines is not None and (two_sided or len(lines) >= 2)) or \
                (proj and proj.n_games >= 20 and proj.opp_games_current >= 10):
            conf = "MEDIUM"
        else:
            conf = "LOW"
            why.append("thin evidence (" + ("stats only, early-season matchup data" if proj else
                                            "one price" if lines else "game log only") + ")")
        if mu_stats is not None and mu_lines is not None and abs(mu_stats / mu_lines - 1) > 0.15:
            conf = "LOW" if conf != "HIGH" else "MEDIUM"
            why.append(f"stats model ({mu_stats:.1f}) and books ({mu_lines:.1f}) disagree by "
                       f"{mu_stats / mu_lines - 1:+.0%}")
        if age_h is not None and age_h > 6:
            conf = "LOW"
            why.append(f"newest line ~{age_h:.0f}h old")
        sd = math.sqrt(mu + mu * mu / r)
        if abs(k - mu) > 2 * sd:
            conf = "LOW" if conf != "HIGH" else "MEDIUM"
            why.append(f"{k}+ is more than 2 SD from the expected {mu:.1f} (tail)")
        if proj and proj.team_changed:
            conf = "LOW"
            why.append("player changed teams or role")
        if mock or any(e.is_mock for e in evs):
            conf = "LOW"
            why.append("MOCK data")

        # ---- 5. explanation
        supporting = [f"Expected {stat}: {mu:.1f} ({basis}). P({k}+) = {p:.0%}; spread of outcomes from {r_src}; "
                      f"{q:.0%} chance of a short-minutes night allowed for."]
        if proj:
            supporting += proj.factors
        opposing = list(proj.warnings) if proj else []
        if mu_stats is not None and mu_lines is not None:
            diff = mu_stats / mu_lines - 1
            (opposing if abs(diff) > 0.15 else supporting).append(
                f"Stats model {mu_stats:.1f} vs sportsbook-implied {mu_lines:.1f} ({diff:+.0%})."
                + (" Books often know about minutes or injuries that box scores don't." if abs(diff) > 0.15 else ""))
        if log and log.newest and (now - log.newest).days > 45:
            opposing.append(f"Most recent logged game is {(now - log.newest).days} days old; role may have changed.")
        if len(books) == 1 and lines:
            opposing.append("Only one sportsbook quoted this prop.")
        if not self.stats:
            opposing.append("No stats database loaded, so no matchup, pace or minutes adjustments "
                            "(run: python -m kalshi_edge build-stats).")
        if g and g.commence_time:
            hrs = (g.commence_time - now).total_seconds() / 3600
            if hrs > 2:
                opposing.append(f"Tip-off in {hrs:.0f}h: injury and minutes news can still move this.")
        opposing += [f"Data note: {n}" for n in notes]

        return Estimate(
            self.name, p, p_lo, p_hi, conf, evs, supporting, opposing,
            assumptions=[
                f"{stat.capitalize()} follow a negative-binomial distribution around the expected value.",
                "Projection constants (matchup half-weight, home +/-1.5%, back-to-back -3%, blowout cut) "
                "are starting values, not fitted.",
                "Vig removed proportionally and by the power method; one-sided prices assume a "
                f"{ONE_SIDED_OVERROUND[0]:.0%}-{ONE_SIDED_OVERROUND[1]:.0%} book margin.",
                "Conditional on the player entering the game (Kalshi settles DNPs at a fair price).",
                f"A {q:.0%} chance of a short night (early injury exit, foul trouble, benched in a blowout) "
                "at about a third of normal output.",
            ] + ([f"Confidence lowered: {'; '.join(why)}."] if why else []),
            would_change=["Injury report / minutes restriction for this player or a teammate.",
                          "Starting lineup announcements (~30 min before tip).",
                          "Line moves on the prop at sharp books."],
            time_sensitive=True, created_at=now,
            params={"kind": "prop", "player": player, "stat": stat, "k": k, "mu": mu, "r": r,
                    "mu_stats": mu_stats, "mu_lines": mu_lines,
                    "team": team, "game": f"{t['date']}:{t['away']}@{t['home']}"})


# ------------------------------------------------------------------ parlay support
def prop_leg_resolver(est: NBAPropEstimator):
    def resolve(leg_ticker: str, side: str, now: datetime):
        t = parse_prop_ticker(leg_ticker)
        if not t:
            return None
        # Leg titles aren't in the leg list; recover the name from the provider by code.
        player = est.props.player_for_code(t["name_code"], t["team"])
        if not player:
            return None
        spec = ContractSpec(leg_ticker, leg_ticker.rsplit("-", 1)[0], t["series"], f"{player}: {t['k']}+",
                            "", "", "", None, "greater", t["k"] - 0.5, None)
        e = est.estimate(spec, {}, now)
        if e.insufficient:
            return None
        p, lo, hi = e.p_yes, e.p_low, e.p_high
        if side == "no":
            p, lo, hi = 1 - p, 1 - hi, 1 - lo
        cond = {"kind": "prop", "player": player, "stat": t["stat"], "side": side,
                "thr": float(t["k"]), "p": p, "lo": lo, "hi": hi}
        return (p, lo, hi, f"NBA props ({player})", e.params["game"], any(x.is_mock for x in e.evidence), cond)
    return resolve


def prop_same_game_joint(conds: list[dict]):
    """Same-player, same-stat YES legs collapse to the strictest one
    (Durant 20+ AND Durant 25+ == Durant 25+). Anything else in the same game
    (two players, or a player plus the team result) is correlated in ways this
    model doesn't capture: returns the collapsed product flagged as inexact."""
    if any(c is None or c.get("kind") != "prop" or c["side"] != "yes" for c in conds):
        return None
    groups: dict = {}
    for c in conds:
        key = (c["player"], c["stat"])
        if key not in groups or c["thr"] > groups[key]["thr"]:
            groups[key] = c
    p = lo = hi = 1.0
    for c in groups.values():
        p, lo, hi = p * c["p"], lo * c["lo"], hi * c["hi"]
    exact = len(groups) == 1
    note = (f"{len(conds)} legs on the same player/stat collapse to the strictest one" if exact else
            f"same-player ladders collapsed; {len(groups)} different player/stat legs multiplied "
            "although they are correlated")
    return p, lo, hi, note, exact
