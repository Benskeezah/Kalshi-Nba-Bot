"""NBA stats database built from ESPN box scores (no API key).

One build gives everything the projection model needs:
  * player game logs: minutes, points, rebounds, assists, opponent, home/away, date
  * team possessions per game -> pace
  * what each defense allowed (points / rebounds / assists per 100 possessions)

    python -m kalshi_edge build-stats            # last season + this season
    python -m kalshi_edge build-stats --update   # just add new games (fast, run daily)

A full first build is ~1,300 box-score requests (10-20 minutes, polite pacing).
Results are cached in SQLite (KALSHI_EDGE_STATS_DB, default nba_stats.sqlite).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..config import settings
from .nba_odds import ESPN_TO_KALSHI, TEAMS
from .nba_props import norm_name

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (event_id TEXT PRIMARY KEY, season INT, date TEXT, home TEXT, away TEXT,
  home_pts REAL, away_pts REAL);
CREATE TABLE IF NOT EXISTS team_games (event_id TEXT, season INT, date TEXT, team TEXT, opp TEXT, home INT,
  pts REAL, fga REAL, fta REAL, oreb REAL, reb REAL, ast REAL, tov REAL, poss REAL, PRIMARY KEY (event_id, team));
CREATE TABLE IF NOT EXISTS player_games (event_id TEXT, season INT, date TEXT, team TEXT, opp TEXT, home INT,
  player TEXT, player_norm TEXT, starter INT, min REAL, pts REAL, reb REAL, ast REAL,
  PRIMARY KEY (event_id, player_norm));
CREATE INDEX IF NOT EXISTS ix_pg_player ON player_games(player_norm, date);
CREATE INDEX IF NOT EXISTS ix_tg_team ON team_games(team, season);
"""


def stats_db_path() -> str:
    return os.environ.get("KALSHI_EDGE_STATS_DB", "nba_stats.sqlite")


def _get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-edge-research"})
    with urllib.request.urlopen(req, timeout=settings().http_timeout) as r:
        return json.loads(r.read().decode())


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pair(v) -> tuple[Optional[float], Optional[float]]:
    """'40-88' -> (40, 88)."""
    try:
        a, b = str(v).split("-")
        return float(a), float(b)
    except ValueError:
        return None, None


# ------------------------------------------------------------------ parsing
def parse_schedule(data: dict) -> list[dict]:
    """Completed regular-season games from a team schedule response."""
    out = []
    for ev in data.get("events", []):
        st = ev.get("seasonType") or {}
        if str(st.get("id")) != "2":
            continue
        comp = (ev.get("competitions") or [{}])[0]
        if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
            continue
        teams = {}
        for c in comp.get("competitors", []):
            code = ESPN_TO_KALSHI.get((c.get("team") or {}).get("abbreviation"))
            score = c.get("score")
            score = score.get("value") if isinstance(score, dict) else score
            teams[c.get("homeAway")] = (code, _num(score))
        if "home" not in teams or "away" not in teams or not teams["home"][0] or not teams["away"][0]:
            continue
        out.append({"event_id": str(ev["id"]), "season": int((ev.get("season") or {}).get("year", 0)),
                    "date": ev.get("date"), "home": teams["home"][0], "away": teams["away"][0],
                    "home_pts": teams["home"][1], "away_pts": teams["away"][1]})
    return out


def parse_summary(data: dict, game: dict) -> tuple[list[dict], list[dict]]:
    """Team rows and player rows from a game summary (box score)."""
    box = data.get("boxscore") or {}
    team_rows, player_rows = [], []
    side_of = {game["home"]: 1, game["away"]: 0}
    pts = {game["home"]: game["home_pts"], game["away"]: game["away_pts"]}
    for t in box.get("teams", []):
        code = ESPN_TO_KALSHI.get((t.get("team") or {}).get("abbreviation"))
        if code not in side_of:
            continue
        s = {x.get("name"): x.get("displayValue") for x in t.get("statistics", [])}
        _, fga = _pair(s.get("fieldGoalsMade-fieldGoalsAttempted"))
        _, fta = _pair(s.get("freeThrowsMade-freeThrowsAttempted"))
        oreb, tov = _num(s.get("offensiveRebounds")), _num(s.get("totalTurnovers") or s.get("turnovers"))
        poss = fga - oreb + tov + 0.44 * fta if None not in (fga, oreb, tov, fta) else None
        opp = game["away"] if code == game["home"] else game["home"]
        team_rows.append({"event_id": game["event_id"], "season": game["season"], "date": game["date"],
                          "team": code, "opp": opp, "home": side_of[code], "pts": pts[code], "fga": fga,
                          "fta": fta, "oreb": oreb, "reb": _num(s.get("totalRebounds")),
                          "ast": _num(s.get("assists")), "tov": tov, "poss": poss})
    for t in box.get("players", []):
        code = ESPN_TO_KALSHI.get((t.get("team") or {}).get("abbreviation"))
        if code not in side_of or not t.get("statistics"):
            continue
        block = t["statistics"][0]
        labels = block.get("labels") or []
        try:
            i_min, i_pts, i_reb, i_ast = (labels.index(x) for x in ("MIN", "PTS", "REB", "AST"))
        except ValueError:
            continue
        opp = game["away"] if code == game["home"] else game["home"]
        for a in block.get("athletes", []):
            stats = a.get("stats") or []
            if a.get("didNotPlay") or len(stats) <= max(i_min, i_pts, i_reb, i_ast):
                continue
            mins = _num(stats[i_min])
            if not mins:
                continue
            name = (a.get("athlete") or {}).get("displayName", "")
            player_rows.append({"event_id": game["event_id"], "season": game["season"], "date": game["date"],
                                "team": code, "opp": opp, "home": side_of[code], "player": name,
                                "player_norm": norm_name(name), "starter": int(bool(a.get("starter"))),
                                "min": mins, "pts": _num(stats[i_pts]), "reb": _num(stats[i_reb]),
                                "ast": _num(stats[i_ast])})
    return team_rows, player_rows


def parse_injuries(data: dict) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list] = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            a = inj.get("athlete") or {}
            code = ESPN_TO_KALSHI.get((a.get("team") or {}).get("abbreviation"))
            if code:
                out.setdefault(code, []).append((a.get("displayName", "?"), inj.get("status", "?")))
    return out


# ------------------------------------------------------------------ database
@dataclass
class TeamProfile:
    team: str
    games: int
    pace: float                   # possessions per game
    allowed_per100: dict          # stat -> per 100 opponent possessions


class StatsDB:
    def __init__(self, path: Optional[str] = None):
        self.path = path or stats_db_path()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- build
    def build(self, seasons: list[int], sleep: float = 0.25, log=print) -> int:
        known = {r[0] for r in self.conn.execute("SELECT event_id FROM games")}
        added = 0
        for season in seasons:
            todo: dict = {}
            for code, (espn, _) in TEAMS.items():
                try:
                    for g in parse_schedule(_get(f"{ESPN}/teams/{espn.lower()}/schedule?season={season}")):
                        if g["event_id"] not in known:
                            todo[g["event_id"]] = g
                except Exception as e:
                    log(f"  schedule {code} {season}: {e.__class__.__name__}: {str(e)[:120]}")
                time.sleep(sleep)
            log(f"Season {season}: {len(todo)} new games to fetch")
            for i, g in enumerate(todo.values(), 1):
                try:
                    self.add_game(g, _get(f"{ESPN}/summary?event={g['event_id']}"))
                    known.add(g["event_id"])
                    added += 1
                except Exception as e:
                    log(f"  game {g['event_id']}: {e.__class__.__name__}: {str(e)[:120]}")
                if i % 100 == 0:
                    log(f"  {i}/{len(todo)}")
                time.sleep(sleep)
        return added

    def add_game(self, game: dict, summary: dict) -> None:
        teams, players = parse_summary(summary, game)
        c = self.conn
        c.execute("INSERT OR REPLACE INTO games VALUES (:event_id,:season,:date,:home,:away,:home_pts,:away_pts)", game)
        c.executemany("INSERT OR REPLACE INTO team_games VALUES (:event_id,:season,:date,:team,:opp,:home,:pts,"
                      ":fga,:fta,:oreb,:reb,:ast,:tov,:poss)", teams)
        c.executemany("INSERT OR REPLACE INTO player_games VALUES (:event_id,:season,:date,:team,:opp,:home,"
                      ":player,:player_norm,:starter,:min,:pts,:reb,:ast)", players)
        c.commit()

    # ---- queries
    def player_games(self, player: str, before: datetime, limit: int = 120) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM player_games WHERE player_norm=? AND date < ? ORDER BY date DESC LIMIT ?",
            (norm_name(player), before.strftime("%Y-%m-%dT%H:%M"), limit)))

    def team_profile(self, team: str, season: int, before: datetime) -> Optional[TeamProfile]:
        """Pace and what this team's DEFENSE allowed, for one season up to `before`."""
        rows = list(self.conn.execute(
            """SELECT o.pts, o.reb, o.ast, o.poss AS opp_poss, t.poss AS own_poss
               FROM team_games t JOIN team_games o ON o.event_id = t.event_id AND o.team = t.opp
               WHERE t.team=? AND t.season=? AND t.date < ? AND o.poss > 0 AND t.poss > 0""",
            (team, season, before.strftime("%Y-%m-%dT%H:%M"))))
        if not rows:
            return None
        n = len(rows)
        opp_poss = sum(r["opp_poss"] for r in rows)
        pace = sum((r["opp_poss"] + r["own_poss"]) / 2 for r in rows) / n
        return TeamProfile(team, n, pace, {s: 100 * sum(r[s] or 0 for r in rows) / opp_poss
                                           for s in ("pts", "reb", "ast")})

    def league_profile(self, season: int, before: datetime) -> Optional[TeamProfile]:
        r = self.conn.execute(
            "SELECT COUNT(*) n, SUM(poss) p, SUM(pts) pts, SUM(reb) reb, SUM(ast) ast FROM team_games "
            "WHERE season=? AND date < ? AND poss > 0", (season, before.strftime("%Y-%m-%dT%H:%M"))).fetchone()
        if not r["n"]:
            return None
        return TeamProfile("LEAGUE", r["n"] // 2, r["p"] / r["n"],
                           {s: 100 * r[s] / r["p"] for s in ("pts", "reb", "ast")})

    def last_game_date(self) -> Optional[str]:
        r = self.conn.execute("SELECT MAX(date) FROM games").fetchone()
        return r[0]


def fetch_injuries() -> dict[str, list[tuple[str, str]]]:
    return parse_injuries(_get(f"{ESPN}/injuries"))
