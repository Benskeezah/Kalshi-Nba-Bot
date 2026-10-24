import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest

from kalshi_edge.estimators.nba_projection import project, season_of
from kalshi_edge.external.nba_stats import StatsDB, parse_injuries, parse_schedule, parse_summary
from kalshi_edge.highprob import Pick, high_prob_picks
from kalshi_edge.edge import analyze
from kalshi_edge.ingestion.kalshi import normalize_market
from kalshi_edge.models import Estimate

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))
import make_mock_stats  # noqa: E402

TIP = datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc)
_DB = None


def db():
    global _DB
    if _DB is None:
        path = os.path.join(tempfile.mkdtemp(), "mock.sqlite")
        make_mock_stats.build(path)
        _DB = StatsDB(path)
    return _DB


def test_season_mapping():
    assert season_of(TIP) == 2027 and season_of(datetime(2026, 3, 1, tzinfo=timezone.utc)) == 2026


def test_parse_schedule_and_summary():
    sched = {"events": [{"id": "1", "date": "2025-10-23T01:47Z", "season": {"year": 2026},
                         "seasonType": {"id": "2"},
                         "competitions": [{"status": {"type": {"completed": True}}, "competitors": [
                             {"homeAway": "home", "team": {"abbreviation": "DAL"}, "score": {"value": 92.0}},
                             {"homeAway": "away", "team": {"abbreviation": "SA"}, "score": {"value": 125.0}}]}]},
                        {"id": "2", "seasonType": {"id": "1"}, "competitions": [{}]}]}
    games = parse_schedule(sched)
    assert len(games) == 1 and games[0]["home"] == "DAL" and games[0]["away"] == "SAS"
    summ = {"boxscore": {
        "teams": [{"team": {"abbreviation": "DAL"}, "statistics": [
            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "40-88"},
            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "16-21"},
            {"name": "offensiveRebounds", "displayValue": "12"}, {"name": "totalTurnovers", "displayValue": "14"},
            {"name": "totalRebounds", "displayValue": "39"}, {"name": "assists", "displayValue": "28"}]}],
        "players": [{"team": {"abbreviation": "DAL"}, "statistics": [{
            "labels": ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST"],
            "athletes": [{"athlete": {"displayName": "Cooper Flagg"}, "starter": True,
                          "stats": ["35", "24", "9-18", "2-5", "4-4", "8", "5"]},
                         {"athlete": {"displayName": "Bench Guy"}, "didNotPlay": True, "stats": []}]}]}]}}
    teams, players = parse_summary(summ, games[0])
    assert teams[0]["poss"] == pytest.approx(88 - 12 + 14 + 0.44 * 21)
    assert players == [players[0]] and players[0]["pts"] == 24 and players[0]["opp"] == "SAS"


def test_parse_injuries():
    data = {"injuries": [{"displayName": "Houston Rockets", "injuries": [
        {"status": "Out", "athlete": {"displayName": "Steven Adams", "team": {"abbreviation": "HOU"}}}]}]}
    assert parse_injuries(data) == {"HOU": [("Steven Adams", "Out")]}


def test_projection_explains_factors_and_reacts_to_matchup():
    p = project(db(), "Fred VanVleet", "assists", "HOU", "DAL", True, TIP, 8.5, {})
    assert p and p.mu_low < p.mu < p.mu_high and p.n_games > 40
    text = " ".join(p.factors)
    for word in ("Minutes", "Rate", "Matchup", "Pace", "Home"):
        assert word in text
    away = project(db(), "Fred VanVleet", "assists", "HOU", "DAL", False, TIP, 8.5, {})
    assert away.mu < p.mu                                         # home boost
    hurt = project(db(), "Fred VanVleet", "assists", "HOU", "DAL", True, TIP, 8.5,
                   {"HOU": [("Kevin Durant", "Out")]})
    assert any("Durant is out" in w for w in hurt.warnings)
    assert project(db(), "Nobody Real", "points", "HOU", "DAL", True, TIP) is None


def _analysis(p, lo, hi, yb, ya, conf="MEDIUM"):
    m = normalize_market({"ticker": "KXNBAPTS-26OCT21DALHOU-HOUKDURANT7-15", "event_ticker": "E",
                          "title": "Kevin Durant: 15+ points", "yes_sub_title": "Kevin Durant: 15+",
                          "yes_bid_dollars": yb, "yes_ask_dollars": ya, "no_bid_dollars": round(1 - ya, 2),
                          "no_ask_dollars": round(1 - yb, 2), "yes_ask_size_fp": 100, "no_ask_size_fp": 100},
                         "t", TIP)
    return analyze(m, Estimate("t", p, lo, hi, conf), 10, 0, TIP)


def test_high_prob_requires_win_rate_and_fair_price():
    good = _analysis(0.92, 0.88, 0.95, 0.83, 0.84)         # 92% to win, costs ~85c
    pricey = _analysis(0.92, 0.88, 0.95, 0.90, 0.91)       # same odds, costs ~92c: not worth it
    coinflip = _analysis(0.55, 0.50, 0.60, 0.40, 0.41)     # cheap but not likely
    picks = high_prob_picks([good, pricey, coinflip])
    assert [p.analysis for p in picks] == [good] and picks[0].side == "YES"
    assert picks[0].breakeven == pytest.approx(0.84 + 0.01, abs=0.001)   # fee 9.4c on 10 -> 10c
    under = _analysis(0.10, 0.06, 0.14, 0.18, 0.20)        # NO side ~90% to win at ~82c
    p = high_prob_picks([under])
    assert p and p[0].side == "NO" and "14 or fewer" in p[0].label
    low = _analysis(0.92, 0.88, 0.95, 0.83, 0.84, conf="LOW")
    assert high_prob_picks([low]) == [] and high_prob_picks([low], allow_low=True)


def test_daily_run_offline(tmp_path):
    from datetime import date
    from kalshi_edge.daily import pick_lines, run_daily
    from kalshi_edge.external.evidence_file import EvidenceFile
    from kalshi_edge.ingestion.kalshi import FixtureKalshiClient
    from kalshi_edge.store import PredictionStore
    fx = os.path.join(os.path.dirname(__file__), "..", "fixtures")
    store = PredictionStore(str(tmp_path / "p.sqlite"))
    out = run_daily(FixtureKalshiClient([os.path.join(fx, "props_2026-10-21_dalhou.json")]), str(tmp_path / "site"),
                    day=date(2026, 10, 21), now=datetime(2026, 10, 21, 22, 15, tzinfo=timezone.utc),
                    stats=db(), evidence=EvidenceFile(os.path.join(fx, "evidence_props.json")), store=store,
                    use_live=False, update_stats=False, stats_mock=True, send=False)
    assert out["mock"] is True and "23 open prop markets" in " ".join(out["log"])
    assert (tmp_path / "site" / "index.html").exists() and (tmp_path / "site" / "picks.json").exists()
    assert len(store.unresolved_tickers()) == 23
    assert out["picks"] == [] or out["picks"][0]["win_prob"] >= 0.7
    assert pick_lines([]) == []


def test_team_change_is_flagged():
    p = project(db(), "Kevin Durant", "points", "DAL", "HOU", True, TIP, None, {})
    assert p.team_changed and any("new team" in w for w in p.warnings)


def test_value_watchlist_rules_and_scoring():
    from kalshi_edge.value import value_picks, value_results
    cheap = _analysis(0.80, 0.72, 0.86, 0.58, 0.60)         # model 80%, costs 60c, 2c spread -> qualifies
    cheap.estimate.params = {"p_book": 0.70}
    wide = _analysis(0.80, 0.72, 0.86, 0.50, 0.60)          # 10c spread -> skipped
    pricey = _analysis(0.92, 0.88, 0.95, 0.83, 0.84)        # too expensive for the watchlist
    vp = value_picks([cheap, wide, pricey])
    assert [v.analysis for v in vp] == [cheap] and vp[0].side == "YES" and vp[0].book == "agrees"
    rows = [{"ticker": "KXNBAPTS-26OCT21DALHOU-HOUKDURANT7-25", "event_ticker": "KXNBAPTS-26OCT21DALHOU",
             "model_prob": 0.80, "outcome": 1, "yes_ask": 0.60, "yes_bid": 0.58, "spread": 0.02,
             "evidence_json": '{"params": {"p_book": 0.55}}'}]
    r = value_results(rows)
    assert r["All watchlist picks"]["n"] == 1 and r["Sportsbook disagrees"]["hit"] == 1.0
    assert r["All watchlist picks"]["result"] == pytest.approx(0.40 - 0.07 * 0.6 * 0.4)
