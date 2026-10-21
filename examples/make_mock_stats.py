"""MOCK stats database for offline demos and tests. Every number is invented.

    python examples/make_mock_stats.py mock_stats.sqlite

The real database comes from:  python -m kalshi_edge build-stats
"""
import math
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/examples/", 1)[0])
from kalshi_edge.external.nba_odds import TEAMS          # noqa: E402
from kalshi_edge.external.nba_props import norm_name     # noqa: E402
from kalshi_edge.external.nba_stats import StatsDB       # noqa: E402

PLAYERS = {   # name: (team, minutes, pts/min, reb/min, ast/min)   -- invented
    "Kevin Durant": ("HOU", 34.5, 0.76, 0.16, 0.13),
    "Fred VanVleet": ("HOU", 33.0, 0.43, 0.11, 0.18),
    "Cooper Flagg": ("DAL", 33.5, 0.63, 0.22, 0.13),
}


def nb(rnd, mu, r):
    lam = rnd.gammavariate(r, mu / r)          # gamma-Poisson mixture
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p < L:
            return k
        k += 1


def build(path: str, seed: int = 4) -> None:
    rnd = random.Random(seed)
    db = StatsDB(path)
    teams = list(TEAMS)
    style = {t: (rnd.uniform(96, 103), rnd.uniform(0.93, 1.07), rnd.uniform(0.94, 1.06), rnd.uniform(0.92, 1.08))
             for t in teams}                   # pace, pts-allowed, reb-allowed, ast-allowed factors
    style["DAL"] = (101.5, 1.04, 1.02, 1.07)   # a generous defence vs assists, for the demo
    eid = 0
    for season, start, n_rounds in ((2026, datetime(2025, 10, 22, tzinfo=timezone.utc), 41),
                                    (2027, datetime(2026, 10, 20, tzinfo=timezone.utc), 1)):
        for rnd_i in range(n_rounds):
            day = start + timedelta(days=rnd_i * 4)
            order = teams[:]
            rnd.shuffle(order)
            for h, a in zip(order[::2], order[1::2]):
                for home, away in ((h, a), (a, h)) if season == 2026 else ((h, a),):
                    eid += 1
                    date = (day + timedelta(days=1 if (home, away) != (h, a) else 0)).strftime("%Y-%m-%dT%H:%MZ")
                    poss = (style[home][0] + style[away][0]) / 2 + rnd.gauss(0, 3)
                    rows = []
                    for team, opp in ((home, away), (away, home)):
                        d = style[opp]
                        rows.append({"team": team, "opp": opp, "poss": poss,
                                     "pts": poss * 1.14 * d[1] + rnd.gauss(0, 9),
                                     "reb": 44 * d[2] + rnd.gauss(0, 5), "ast": 26 * d[3] + rnd.gauss(0, 4)})
                    game = {"event_id": f"M{eid}", "season": season, "date": date, "home": home, "away": away,
                            "home_pts": rows[0]["pts"], "away_pts": rows[1]["pts"]}
                    db.conn.execute("INSERT OR REPLACE INTO games VALUES (:event_id,:season,:date,:home,:away,"
                                    ":home_pts,:away_pts)", game)
                    for r in rows:
                        db.conn.execute("INSERT OR REPLACE INTO team_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (game["event_id"], season, date, r["team"], r["opp"], int(r["team"] == home),
                                         r["pts"], 88, 22, 11, r["reb"], r["ast"], 14, r["poss"]))
                    for name, (team, mins, pr, rr, ar) in PLAYERS.items():
                        if team not in (home, away) or rnd.random() < 0.06:
                            continue
                        opp = away if team == home else home
                        d = style[opp]
                        m = max(8.0, rnd.gauss(mins, 3.5))
                        db.conn.execute("INSERT OR REPLACE INTO player_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (game["event_id"], season, date, team, opp, int(team == home), name,
                                         norm_name(name), 1, round(m),
                                         nb(rnd, m * pr * (1 + 0.5 * (d[1] - 1)), 14),
                                         nb(rnd, m * rr * (1 + 0.5 * (d[2] - 1)), 25),
                                         nb(rnd, m * ar * (1 + 0.5 * (d[3] - 1)), 30)))
    db.conn.commit()


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "mock_stats.sqlite")
    print("MOCK stats database written (invented numbers).")
