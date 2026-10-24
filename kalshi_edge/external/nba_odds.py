"""NBA sportsbook lines: the main independent evidence for NBA markets.

Sportsbooks are a separate market from Kalshi with far more money behind
NBA lines, so their de-vigged prices are a strong, independent estimate.
Sources, in order of quality:
  * The Odds API (needs ODDS_API_KEY, free tier ~500 requests/month):
    many books including sharp ones (Pinnacle via the "eu" region).
  * ESPN scoreboard (no key): one book (DraftKings), sometimes missing.
  * Lines you type into the evidence file under "nba_lines".
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from ..config import settings

# Kalshi team code -> (ESPN abbreviation, full name). Kalshi codes come from tickers.
TEAMS: dict[str, tuple[str, str]] = {
    "ATL": ("ATL", "Atlanta Hawks"), "BOS": ("BOS", "Boston Celtics"), "BKN": ("BKN", "Brooklyn Nets"),
    "CHA": ("CHA", "Charlotte Hornets"), "CHI": ("CHI", "Chicago Bulls"), "CLE": ("CLE", "Cleveland Cavaliers"),
    "DAL": ("DAL", "Dallas Mavericks"), "DEN": ("DEN", "Denver Nuggets"), "DET": ("DET", "Detroit Pistons"),
    "GSW": ("GS", "Golden State Warriors"), "HOU": ("HOU", "Houston Rockets"), "IND": ("IND", "Indiana Pacers"),
    "LAC": ("LAC", "LA Clippers"), "LAL": ("LAL", "Los Angeles Lakers"), "MEM": ("MEM", "Memphis Grizzlies"),
    "MIA": ("MIA", "Miami Heat"), "MIL": ("MIL", "Milwaukee Bucks"), "MIN": ("MIN", "Minnesota Timberwolves"),
    "NOP": ("NO", "New Orleans Pelicans"), "NYK": ("NY", "New York Knicks"), "OKC": ("OKC", "Oklahoma City Thunder"),
    "ORL": ("ORL", "Orlando Magic"), "PHI": ("PHI", "Philadelphia 76ers"), "PHX": ("PHX", "Phoenix Suns"),
    "POR": ("POR", "Portland Trail Blazers"), "SAC": ("SAC", "Sacramento Kings"), "SAS": ("SA", "San Antonio Spurs"),
    "TOR": ("TOR", "Toronto Raptors"), "UTA": ("UTAH", "Utah Jazz"), "WAS": ("WSH", "Washington Wizards"),
}
ESPN_TO_KALSHI = {v[0]: k for k, v in TEAMS.items()}
NICK_TO_KALSHI = {v[1].lower().split()[-1]: k for k, v in TEAMS.items()}   # "celtics" -> BOS


def team_code(name: str):
    """Kalshi code from any full team name ("LA Clippers", "Los Angeles Clippers", ...)."""
    return NICK_TO_KALSHI.get((name or "").strip().lower().split()[-1]) if name else None


NAME_TO_KALSHI = {v[1].lower(): k for k, v in TEAMS.items()}
# Kalshi titles use city names ("Los Angeles L", "New York"); used to match parlay legs.
CITY_TO_KALSHI = {
    "atlanta": "ATL", "boston": "BOS", "brooklyn": "BKN", "charlotte": "CHA", "chicago": "CHI",
    "cleveland": "CLE", "dallas": "DAL", "denver": "DEN", "detroit": "DET", "golden state": "GSW",
    "houston": "HOU", "indiana": "IND", "los angeles c": "LAC", "los angeles l": "LAL", "memphis": "MEM",
    "miami": "MIA", "milwaukee": "MIL", "minnesota": "MIN", "new orleans": "NOP", "new york": "NYK",
    "oklahoma city": "OKC", "orlando": "ORL", "philadelphia": "PHI", "phoenix": "PHX", "portland": "POR",
    "sacramento": "SAC", "san antonio": "SAS", "toronto": "TOR", "utah": "UTA", "washington": "WAS",
}


@dataclass
class BookLine:
    """One sportsbook's prices for one game. American odds; lines from the HOME side."""
    book: str
    updated_at: Optional[datetime]
    ml_home: Optional[float] = None
    ml_away: Optional[float] = None
    spread_home: Optional[float] = None          # e.g. -7.5 means home favoured by 7.5
    spread_home_odds: Optional[float] = None
    spread_away_odds: Optional[float] = None
    total: Optional[float] = None
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None
    source: str = ""
    url: Optional[str] = None
    is_mock: bool = False


@dataclass
class GameOdds:
    home: str                                    # Kalshi codes
    away: str
    commence_time: Optional[datetime]
    books: list[BookLine] = field(default_factory=list)

    def has(self, team: str) -> bool:
        return team in (self.home, self.away)


def _ts(s) -> Optional[datetime]:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "kalshi-edge-research"})
    with urllib.request.urlopen(req, timeout=settings().http_timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- parsers
def parse_espn_scoreboard(data: dict, fetched_at: datetime) -> list[GameOdds]:
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c.get("team", {}).get("abbreviation") for c in comp.get("competitors", [])}
        home, away = ESPN_TO_KALSHI.get(teams.get("home")), ESPN_TO_KALSHI.get(teams.get("away"))
        if not home or not away:
            continue
        g = GameOdds(home, away, _ts(comp.get("date") or ev.get("date")))
        for o in comp.get("odds") or []:
            ml = o.get("moneyline") or {}
            ps = o.get("pointSpread") or {}
            tot = o.get("total") or {}

            def close(d, side, key):
                v = ((d.get(side) or {}).get("close") or {}).get(key)
                try:
                    return float(str(v).replace("o", "").replace("u", "")) if v not in (None, "", "OFF", "EVEN") \
                        else (100.0 if v == "EVEN" else None)
                except ValueError:
                    return None
            g.books.append(BookLine(
                book=(o.get("provider") or {}).get("name", "ESPN odds"), updated_at=fetched_at,
                ml_home=close(ml, "home", "odds"), ml_away=close(ml, "away", "odds"),
                spread_home=close(ps, "home", "line") if ps else _f(o.get("spread")),
                spread_home_odds=close(ps, "home", "odds"), spread_away_odds=close(ps, "away", "odds"),
                total=close(tot, "over", "line") if tot else _f(o.get("overUnder")),
                over_odds=close(tot, "over", "odds"), under_odds=close(tot, "under", "odds"),
                source="ESPN scoreboard", url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"))
        out.append(g)
    return out


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_odds_api(data: list) -> list[GameOdds]:
    out = []
    for ev in data:
        home, away = team_code(ev["home_team"]), team_code(ev["away_team"])
        if not home or not away:
            continue
        g = GameOdds(home, away, _ts(ev.get("commence_time")))
        for bk in ev.get("bookmakers", []):
            bl = BookLine(book=bk.get("title", bk.get("key")), updated_at=_ts(bk.get("last_update")),
                          source="The Odds API", url="https://the-odds-api.com")
            for mk in bk.get("markets", []):
                oc = {o["name"]: o for o in mk.get("outcomes", [])}
                hn, an = ev["home_team"], ev["away_team"]
                if mk["key"] == "h2h" and hn in oc and an in oc:
                    bl.ml_home, bl.ml_away = oc[hn]["price"], oc[an]["price"]
                elif mk["key"] == "spreads" and hn in oc and an in oc:
                    bl.spread_home, bl.spread_home_odds = oc[hn].get("point"), oc[hn]["price"]
                    bl.spread_away_odds = oc[an]["price"]
                elif mk["key"] == "totals" and "Over" in oc and "Under" in oc:
                    bl.total, bl.over_odds, bl.under_odds = oc["Over"].get("point"), oc["Over"]["price"], oc["Under"]["price"]
            g.books.append(bl)
        out.append(g)
    return out


def parse_evidence_lines(entries: list[dict], mock: bool) -> list[GameOdds]:
    """Evidence-file format: {"home": "BOS", "away": "NYK", "tip": iso, "book": ..., "as_of": iso,
    "ml_home": -130, "ml_away": 110, "spread_home": -2.5, "spread_home_odds": -110, ...}"""
    games: dict[tuple, GameOdds] = {}
    for e in entries:
        key = (e["home"], e["away"])
        g = games.setdefault(key, GameOdds(e["home"], e["away"], _ts(e.get("tip"))))
        g.books.append(BookLine(
            book=e["book"], updated_at=_ts(e.get("as_of")),
            **{k: _f(e.get(k)) for k in ("ml_home", "ml_away", "spread_home", "spread_home_odds",
                                         "spread_away_odds", "total", "over_odds", "under_odds")},
            source=e.get("source", "evidence file"), url=e.get("url"), is_mock=mock))
    return list(games.values())


# ---------------------------------------------------------------- provider
class NBAOddsProvider:
    """Collects lines from every available source and caches them per run."""

    def __init__(self, use_live: bool = True, evidence_lines: Optional[list[dict]] = None,
                 evidence_mock: bool = False):
        self.use_live = use_live
        self.notes: list[str] = []
        self._games: list[GameOdds] = parse_evidence_lines(evidence_lines or [], evidence_mock)
        self._fetched: set[date] = set()

    def _fetch(self, d: date) -> None:
        if not self.use_live or d in self._fetched:
            return
        self._fetched.add(d)
        now = datetime.now(timezone.utc)
        try:
            data = _get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates="
                        + d.strftime("%Y%m%d"))
            self._merge(parse_espn_scoreboard(data, now))
        except Exception as e:
            self.notes.append(f"ESPN odds fetch failed: {e.__class__.__name__}")
        key = os.environ.get("ODDS_API_KEY")
        if key and "oddsapi" not in self._fetched:
            self._fetched.add("oddsapi")  # one call covers every upcoming game
            q = urllib.parse.urlencode({"apiKey": key, "regions": "us,eu", "oddsFormat": "american",
                                        "markets": "h2h,spreads,totals"})
            try:
                self._merge(parse_odds_api(_get(f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds?{q}")))
            except Exception as e:
                self.notes.append(f"The Odds API fetch failed: {e.__class__.__name__}")
        elif not key:
            self.notes.append("ODDS_API_KEY not set: only ESPN's single book is available live.")

    def _merge(self, games: list[GameOdds]) -> None:
        for g in games:
            same = self._find(g.home, g.away, g.commence_time)
            if same:
                same.books.extend(b for b in g.books if b.book not in {x.book for x in same.books})
            else:
                self._games.append(g)

    def _find(self, a: str, b: str, tip: Optional[datetime]) -> Optional[GameOdds]:
        for g in self._games:
            if {g.home, g.away} == {a, b} and (tip is None or g.commence_time is None
                                               or abs((g.commence_time - tip).total_seconds()) < 36 * 3600):
                return g
        return None

    def game(self, team_a: str, team_b: str, game_date: date) -> Optional[GameOdds]:
        self._fetch(game_date)
        from datetime import timedelta
        self._fetch(game_date + timedelta(days=1))   # late tips fall on the next UTC day
        tip = datetime(game_date.year, game_date.month, game_date.day, 23, tzinfo=timezone.utc)
        return self._find(team_a, team_b, tip)

    def games_for_team(self, team: str) -> list[GameOdds]:
        return [g for g in self._games if g.has(team)]
