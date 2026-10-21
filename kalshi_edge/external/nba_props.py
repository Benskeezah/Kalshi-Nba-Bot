"""Player-prop evidence for NBA markets.

Sources:
  * The Odds API (ODDS_API_KEY): player_points / player_rebounds / player_assists
    lines from many books, including alternate lines where offered. Each event
    costs extra request credits; the provider fetches each game at most once.
  * ESPN game logs (no key): the player's recent box scores, used as a SECOND
    opinion. Logs don't know about tonight's injuries, matchup or role changes.
  * Lines you type into the evidence file under "nba_props". One-sided prices
    (only the under, say) are allowed and get a wider range.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import settings
from .nba_odds import TEAMS

STATS = {"points": "PTS", "rebounds": "REB", "assists": "AST"}
ODDS_API_MARKETS = {"points": "player_points", "rebounds": "player_rebounds", "assists": "player_assists"}


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def _ts(s) -> Optional[datetime]:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-edge-research"})
    with urllib.request.urlopen(req, timeout=settings().http_timeout) as r:
        return json.loads(r.read().decode())


@dataclass
class PropLine:
    player: str
    stat: str                     # points / rebounds / assists
    line: float                   # e.g. 24.5
    over: Optional[float]         # American odds; None if not quoted
    under: Optional[float]
    book: str
    updated_at: Optional[datetime]
    source: str = ""
    url: Optional[str] = None
    is_mock: bool = False


@dataclass
class GameLog:
    player: str
    stat: str
    values: list[float]           # newest first, games actually played
    minutes: list[float]
    season_label: str
    source: str
    url: Optional[str] = None
    is_mock: bool = False
    newest: Optional[datetime] = None


@dataclass
class _Cache:
    lines: list[PropLine] = field(default_factory=list)
    logs: dict = field(default_factory=dict)


# ---------------------------------------------------------------- parsers
def parse_odds_api_props(event: dict) -> list[PropLine]:
    out = []
    rev = {v: k for k, v in ODDS_API_MARKETS.items()}
    for bk in event.get("bookmakers", []):
        for mk in bk.get("markets", []):
            stat = rev.get(mk["key"].replace("_alternate", ""))
            if not stat:
                continue
            pairs: dict = {}
            for o in mk.get("outcomes", []):
                key = (o.get("description", ""), o.get("point"))
                pairs.setdefault(key, {})[o["name"].lower()] = o["price"]
            for (player, point), sides in pairs.items():
                if point is None or not player:
                    continue
                out.append(PropLine(player, stat, float(point), sides.get("over"), sides.get("under"),
                                    bk.get("title", bk.get("key")), _ts(mk.get("last_update") or bk.get("last_update")),
                                    "The Odds API", "https://the-odds-api.com"))
    return out


def parse_espn_gamelog(data: dict, player: str, stat: str) -> Optional[GameLog]:
    """ESPN gamelog JSON: 'labels' names the stat columns; per-game rows live in
    seasonTypes[].categories[].events[] as {eventId, stats[]}. Regular season only."""
    labels = data.get("labels") or []
    want = STATS[stat]
    if want not in labels or "MIN" not in labels:
        return None
    si, mi = labels.index(want), labels.index("MIN")
    meta = data.get("events") or {}
    rows = []
    for st in data.get("seasonTypes") or []:
        if "regular" not in str(st.get("displayName", "")).lower():
            continue
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                stats = ev.get("stats") or []
                try:
                    v, mins = float(stats[si]), float(stats[mi])
                except (IndexError, ValueError):
                    continue
                when = _ts((meta.get(str(ev.get("eventId"))) or {}).get("gameDate"))
                rows.append((when, v, mins))
    rows = [r for r in rows if r[2] > 0]
    if not rows:
        return None
    rows.sort(key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    label = next((st.get("displayName") for st in data.get("seasonTypes") or []
                  if "regular" in str(st.get("displayName", "")).lower()), "regular season")
    return GameLog(player, stat, [r[1] for r in rows], [r[2] for r in rows], label, "ESPN game log",
                   newest=rows[0][0])


# ---------------------------------------------------------------- provider
class PropProvider:
    def __init__(self, use_live: bool = True, evidence_props: Optional[list[dict]] = None,
                 evidence_logs: Optional[dict] = None, mock: bool = False):
        self.use_live = use_live
        self.notes: list[str] = []
        self._c = _Cache()
        self._done: set = set()
        for e in evidence_props or []:
            self._c.lines.append(PropLine(
                e["player"], e["stat"], float(e["line"]), e.get("over"), e.get("under"), e["book"],
                _ts(e.get("as_of")), e.get("source", "evidence file"), e.get("url"), mock))
        for player, stats in (evidence_logs or {}).items():
            for stat, vals in stats.items():
                if stat in STATS:
                    self._c.logs[(norm_name(player), stat)] = GameLog(
                        player, stat, list(vals), [], stats.get("season", "?"),
                        stats.get("source", "evidence file"), stats.get("url"), mock,
                        _ts(stats.get("newest_game")))

    # -- sportsbook lines
    def lines(self, player: str, stat: str, home: str, away: str, tip: Optional[datetime]) -> list[PropLine]:
        self._fetch_odds_api(home, away)
        n = norm_name(player)
        return [l for l in self._c.lines if l.stat == stat and norm_name(l.player) == n]

    # Which games may spend Odds API credits. None = every game; an empty set = none.
    odds_games: Optional[set] = None
    quota_remaining: Optional[str] = None

    def _fetch_odds_api(self, home: str, away: str) -> None:
        key = os.environ.get("ODDS_API_KEY")
        tag = ("oa", home, away)
        if not self.use_live or tag in self._done:
            return
        if self.odds_games is not None and frozenset((home, away)) not in self.odds_games:
            return
        self._done.add(tag)
        if not key:
            self.notes.append("ODDS_API_KEY not set: no live sportsbook prop lines.")
            return
        try:
            base = "https://api.the-odds-api.com/v4/sports/basketball_nba"
            events = _get(f"{base}/events?" + urllib.parse.urlencode({"apiKey": key}))
            names = {TEAMS[home][1].lower(), TEAMS[away][1].lower()}
            ev = next((e for e in events if {e["home_team"].lower(), e["away_team"].lower()} == names), None)
            if not ev:
                self.notes.append("The Odds API has no event for this game yet.")
                return
            # Credits used ~= markets x regions per game, so keep both small by default.
            markets = list(ODDS_API_MARKETS.values())
            if os.environ.get("ODDS_API_ALTERNATES") == "1":
                markets += [m + "_alternate" for m in ODDS_API_MARKETS.values()]
            q = urllib.parse.urlencode({"apiKey": key, "regions": os.environ.get("ODDS_API_REGIONS", "us"),
                                        "markets": ",".join(markets), "oddsFormat": "american"})
            req = urllib.request.Request(f"{base}/events/{ev['id']}/odds?{q}",
                                         headers={"Accept": "application/json", "User-Agent": "kalshi-edge-research"})
            with urllib.request.urlopen(req, timeout=settings().http_timeout) as r:
                self.quota_remaining = r.headers.get("x-requests-remaining")
                self._c.lines += parse_odds_api_props(json.loads(r.read().decode()))
        except Exception as e:
            self.notes.append(f"The Odds API prop fetch failed: {e.__class__.__name__}")

    # -- names
    def player_for_code(self, code: str, team: str) -> Optional[str]:
        """Kalshi leg tickers name players like 'KDURANT' (first initial + last name)."""
        def code_of(full):
            parts = [p for p in re.split(r"[\s.]+", full) if p and norm_name(p)]
            return (norm_name(parts[0])[:1] + norm_name("".join(parts[1:]))).upper() if len(parts) > 1 else ""
        known = {l.player for l in self._c.lines} | {g.player for g in self._c.logs.values()}
        for name in known:
            if code_of(name) == code:
                return name
        if not self.use_live:
            return None
        try:
            roster = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/"
                          f"{TEAMS[team][0].lower()}/roster")
            return next((a["fullName"] for a in roster.get("athletes", []) if code_of(a.get("fullName", "")) == code), None)
        except Exception:
            return None

    # -- game logs
    def log(self, player: str, stat: str, team: str, season: int) -> Optional[GameLog]:
        """Current season plus last season, newest games first."""
        k = (norm_name(player), stat)
        if k in self._c.logs or not self.use_live or ("log",) + k in self._done:
            return self._c.logs.get(k)
        self._done.add(("log",) + k)
        try:
            roster = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/"
                          f"{TEAMS[team][0].lower()}/roster")
            ath = next((a for a in roster.get("athletes", []) if norm_name(a.get("fullName", "")) == k[0]), None)
            if not ath:
                self.notes.append(f"ESPN roster has no player named {player}.")
                return None
            per_stat: dict = {}
            for yr in (season, season - 1):
                url = (f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/"
                       f"{ath['id']}/gamelog?season={yr}")
                try:
                    data = _get(url)
                except Exception:
                    continue
                for s in STATS:
                    g = parse_espn_gamelog(data, player, s)
                    if not g:
                        continue
                    if s in per_stat:
                        prev = per_stat[s]
                        prev.values += g.values
                        prev.minutes += g.minutes
                        prev.season_label += f" + {g.season_label}"
                    else:
                        g.url = url
                        per_stat[s] = g
            for s, g in per_stat.items():
                self._c.logs[(k[0], s)] = g
        except Exception as e:
            self.notes.append(f"ESPN game log fetch failed: {e.__class__.__name__}")
        return self._c.logs.get(k)
