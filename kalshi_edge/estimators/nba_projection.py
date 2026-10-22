"""Stats-based projection of a player's points / rebounds / assists for one game.

    expected = projected minutes x per-minute rate x matchup x pace x home/away

  minutes  recent minutes (newest games weigh most), cut for back-to-backs and for
           likely blowouts (starters sit late in lopsided games)
  rate     production per minute over his recent games; last season counts less
  matchup  how much the opponent's defense allowed of this stat per 100 possessions
           vs league average. Only HALF the measured effect is applied, because a
           team's defence moves an individual player less than its team totals.
           Early in a season this leans on last season's defence (rosters change).
  pace     expected possessions in this game vs his own team's usual pace
  home     +/-1.5%

Every factor is reported back so you can see why the number is what it is.
All the constants below are reasonable starting points, not fitted values; the
calibration log is how to tune them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..external.nba_stats import StatsDB, TeamProfile
from ..user_settings import load as load_settings

# Matchup weight, home boost and back-to-back cut live in picks_settings.toml.
OPP_PRIOR_GAMES = 15          # current-season games needed to trust this season's defence half-way
STAT_COL = {"points": "pts", "rebounds": "reb", "assists": "ast"}


@dataclass
class Projection:
    mu: float
    mu_low: float
    mu_high: float
    values: list[float]                      # this stat, newest first (for spread estimate)
    n_games: int
    n_current: int
    minutes: float
    rate: float
    factors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    opp_games_current: int = 0
    team_changed: bool = False


def season_of(d: datetime) -> int:
    """ESPN season year: the 2026-27 season is 2027."""
    return d.year + 1 if d.month >= 8 else d.year


def _wmean(pairs, half_life):
    num = den = 0.0
    for i, (v, w0) in enumerate(pairs):
        w = w0 * 0.5 ** (i / half_life)
        num += v * w
        den += w
    return num / den if den else None


def _blend(cur: Optional[TeamProfile], prev: Optional[TeamProfile]):
    """Weight this season's profile by games played; fall back to last season."""
    if cur and prev:
        w = cur.games / (cur.games + OPP_PRIOR_GAMES)
        return w, cur, prev
    if cur:
        return 1.0, cur, None
    if prev:
        return 0.0, None, prev
    return None, None, None


def _ratio(stat: str, w, cur, prev, lg_cur, lg_prev) -> Optional[float]:
    parts = []
    if cur and lg_cur:
        parts.append((w, cur.allowed_per100[stat] / lg_cur.allowed_per100[stat]))
    if prev and lg_prev:
        parts.append((1 - w if cur else 1.0, prev.allowed_per100[stat] / lg_prev.allowed_per100[stat]))
    tot = sum(x for x, _ in parts)
    return sum(x * r for x, r in parts) / tot if tot else None


def _pace(w, cur, prev) -> Optional[float]:
    if cur and prev:
        return w * cur.pace + (1 - w) * prev.pace
    return (cur or prev).pace if (cur or prev) else None


def project(db: StatsDB, player: str, stat: str, team: str, opp: str, home: bool, tip: datetime,
            spread_abs: Optional[float] = None, injuries: Optional[dict] = None) -> Optional[Projection]:
    col = STAT_COL[stat]
    S = season_of(tip)
    cfg = load_settings()
    matchup_w = cfg["matchup_weight"]
    home_boost = cfg["home_court_percent"] / 100
    b2b = 1 - cfg["back_to_back_minutes_percent"] / 100
    rows = [r for r in db.player_games(player, tip, 120) if r[col] is not None]
    if len(rows) < 5:
        return None
    cur_n = sum(1 for r in rows if r["season"] == S)
    # Last season's games count less once this season has a few.
    sw = lambda r: 1.0 if r["season"] == S else (0.5 if cur_n >= 3 else 0.9)
    factors, warns = [], []

    # ---- minutes
    mins = _wmean([(r["min"], sw(r)) for r in rows[:20]], 6)
    last10 = sum(r["min"] for r in rows[:10]) / min(10, len(rows))
    starter = sum(r["starter"] for r in rows[:10]) >= 5
    m_adj, notes = 1.0, []
    last_date = datetime.fromisoformat(rows[0]["date"].replace("Z", "+00:00"))
    if tip - last_date < timedelta(hours=30):
        m_adj *= b2b
        notes.append(f"back-to-back {b2b - 1:+.0%}")
    if spread_abs is not None and starter and spread_abs > 6:
        cut = max(0.92, 1 - 0.006 * (spread_abs - 6))
        m_adj *= cut
        notes.append(f"blowout risk (spread {spread_abs:g}) {cut - 1:+.0%}")
    minutes = mins * m_adj
    factors.append(f"Minutes: {minutes:.1f} projected (recent weighted {mins:.1f}, last-10 avg {last10:.1f}"
                   + (f"; {', '.join(notes)}" if notes else "") + ").")
    moved = rows[0]["team"] != team
    recent_other = sum(1 for r in rows[:20] if r["team"] != team)
    if moved or recent_other >= 10:
        warns.append(f"{player}'s logged games are mostly for {rows[0]['team']}, not {team}: new team or "
                     "role, so his old minutes and usage may not carry over.")
    if (tip - last_date).days > 60:
        warns.append(f"Last logged game was {(tip - last_date).days} days ago; minutes and role may have changed.")

    # ---- per-minute rate
    use = rows[:80]
    num = sum(r[col] * sw(r) * 0.5 ** (i / 20) for i, r in enumerate(use))
    den = sum(r["min"] * sw(r) * 0.5 ** (i / 20) for i, r in enumerate(use))
    rate = num / den
    rate_se = 1.5 / math.sqrt(max(sum(r[col] for r in use), 1.0))     # relative, widened for drift
    factors.append(f"Rate: {rate * 36:.1f} {stat} per 36 min over his last {len(use)} games "
                   f"({cur_n} this season).")

    # ---- matchup and pace
    opp_cur, opp_prev = db.team_profile(opp, S, tip), db.team_profile(opp, S - 1, tip)
    own_cur, own_prev = db.team_profile(team, S, tip), db.team_profile(team, S - 1, tip)
    lg_cur, lg_prev = db.league_profile(S, tip), db.league_profile(S - 1, tip)
    w_opp, oc, op = _blend(opp_cur, opp_prev)
    f_opp = 1.0
    if w_opp is not None:
        ratio = _ratio(col, w_opp, oc, op, lg_cur, lg_prev)
        if ratio:
            f_opp = 1 + matchup_w * (ratio - 1)
            src = (f"{opp_cur.games} games this season" if opp_cur else "") + \
                  (" + " if opp_cur and opp_prev else "") + ("last season" if opp_prev else "")
            factors.append(f"Matchup: {opp} allows {ratio - 1:+.0%} {stat} per 100 possessions vs league "
                           f"average ({src}) -> {f_opp - 1:+.1%} applied ({matchup_w:.0%} weight).")
        if not opp_cur or opp_cur.games < 5:
            warns.append(f"{opp}'s defence this season is a small or empty sample; the matchup factor "
                         "leans on last season, before roster changes.")
    else:
        warns.append(f"No defensive data for {opp}; matchup not applied.")
    f_pace = 1.0
    w_own, tc, tp = _blend(own_cur, own_prev)
    if w_opp is not None and w_own is not None:
        opp_pace, own_pace = _pace(w_opp, oc, op), _pace(w_own, tc, tp)
        if opp_pace and own_pace:
            f_pace = ((own_pace + opp_pace) / 2) / own_pace
            factors.append(f"Pace: expected {(own_pace + opp_pace) / 2:.1f} possessions vs {team}'s usual "
                           f"{own_pace:.1f} -> {f_pace - 1:+.1%}.")
    f_home = 1 + (home_boost if home else -home_boost)
    factors.append(f"{'Home' if home else 'Away'}: {f_home - 1:+.1%}.")

    # ---- injuries (flags only; not converted into numbers)
    for name, status in (injuries or {}).get(team, []):
        if name.lower() == player.lower():
            warns.append(f"{player} is listed {status} on the injury report.")
        elif status.lower() == "out":
            warns.append(f"Teammate {name} is out; {player}'s usage may be higher than his history shows.")

    mu = minutes * rate * f_opp * f_pace * f_home
    lo = mu * (1 - rate_se) * 0.92 / max(f_opp, 1.0)
    hi = mu * (1 + rate_se) * 1.08 / min(f_opp, 1.0)
    if moved or recent_other >= 10:            # much less certain about the role
        lo, hi = lo * 0.75, hi * 1.25
    p = Projection(mu, lo, hi, [r[col] for r in rows], len(rows), cur_n, minutes, rate,
                   factors, warns, opp_cur.games if opp_cur else 0)
    p.team_changed = moved or recent_other >= 10
    return p
