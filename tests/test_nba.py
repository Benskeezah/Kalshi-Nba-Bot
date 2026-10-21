from datetime import datetime, timezone

import pytest

from kalshi_edge.estimators.nba import (NBAEstimator, american_to_prob, devig, nba_same_game_joint,
                                        parse_nba_ticker)
from kalshi_edge.estimators.registry import EstimatorRegistry
from kalshi_edge.external.evidence_file import EvidenceFile
from kalshi_edge.external.nba_odds import NBAOddsProvider, parse_espn_scoreboard, parse_odds_api
from kalshi_edge.ingestion.kalshi import normalize_market

NOW = datetime(2026, 10, 21, 21, 45, tzinfo=timezone.utc)
LINES = [{"home": "SAS", "away": "HOU", "tip": "2026-10-24T01:30:00Z", "book": "DraftKings",
          "as_of": "2026-10-21T21:40:00Z", "ml_home": -218, "ml_away": 180, "spread_home": -7.5,
          "spread_home_odds": -105, "spread_away_odds": -115, "total": 225.5,
          "over_odds": -110, "under_odds": -110}]


def spec(ticker, floor=None, st="structured", legs=()):
    m = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "strike_type": st,
         "floor_strike": floor, "mve_selected_legs": [{"market_ticker": t, "side": s} for t, s in legs]}
    return normalize_market(m, "t", NOW).contract_spec()


def est():
    return NBAEstimator(NBAOddsProvider(False, LINES))


def test_odds_maths():
    assert american_to_prob(-130) == pytest.approx(130 / 230)
    assert american_to_prob(110) == pytest.approx(100 / 210)
    assert devig(-110, -110) == pytest.approx(0.5)
    assert devig(-218, 180) == pytest.approx(0.6575, abs=1e-3)
    # power method shades the underdog down relative to proportional
    assert devig(180, -218, "power") < devig(180, -218)


def test_ticker_parse():
    t = parse_nba_ticker("KXNBASPREAD-26OCT23HOUSAS-SAS8")
    assert (t["away"], t["home"], t["team"], t["strike_code"]) == ("HOU", "SAS", "SAS", "8")
    assert parse_nba_ticker("KXNBATOTAL-26OCT23HOUSAS-254")["strike_code"] == "254"


def test_winner_both_sides_sum_to_one():
    a = est().estimate(spec("KXNBAGAME-26OCT23HOUSAS-SAS"), {}, NOW)
    b = est().estimate(spec("KXNBAGAME-26OCT23HOUSAS-HOU"), {}, NOW)
    assert a.p_yes + b.p_yes == pytest.approx(1.0)
    assert a.p_yes == pytest.approx(0.6575, abs=0.002)
    assert a.p_low < a.p_yes < a.p_high


def test_margin_at_main_line_near_cover_price():
    e = est().estimate(spec("KXNBASPREAD-26OCT23HOUSAS-SAS8", 7.5, "greater"), {}, NOW)
    assert e.p_yes == pytest.approx(devig(-105, -115), abs=0.002)
    far = est().estimate(spec("KXNBASPREAD-26OCT23HOUSAS-SAS34", 33.5, "greater"), {}, NOW)
    assert far.p_yes < 0.05 and far.confidence == "LOW"


def test_total_at_main_line_is_half():
    e = est().estimate(spec("KXNBATOTAL-26OCT23HOUSAS-226", 225.5, "greater"), {}, NOW)
    assert e.p_yes == pytest.approx(0.5, abs=0.002)


def test_insufficient_cases():
    after_tip = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
    assert est().estimate(spec("KXNBAGAME-26OCT23HOUSAS-SAS"), {}, after_tip).insufficient
    assert est().estimate(spec("KXNBAGAME-26OCT23LACLAL-LAL"), {}, NOW).insufficient


def test_same_game_parlay_uses_joint_not_product():
    reg = EstimatorRegistry(False, EvidenceFile(data={"nba_lines": LINES}))
    win = "KXNBAGAME-26OCT23HOUSAS-SAS"
    cover = "KXNBASPREAD-26OCT23HOUSAS-SAS8"
    p_cover = reg.estimate(spec(cover, 7.5, "greater"), {}, NOW).p_yes
    par = reg.estimate(spec("KXMVEX-1", legs=[(win, "yes"), (cover, "yes")]), {}, NOW)
    assert par.p_yes == pytest.approx(p_cover)            # not p_win * p_cover
    both = reg.estimate(spec("KXMVEX-2", legs=[(win, "yes"), ("KXNBAGAME-26OCT23HOUSAS-HOU", "yes")]), {}, NOW)
    assert both.p_yes == 0.0


def test_parlay_with_unknown_sport_leg_is_insufficient():
    reg = EstimatorRegistry(False, EvidenceFile(data={"nba_lines": LINES}))
    s = spec("KXMVEX-3", legs=[("KXNBAGAME-26OCT23HOUSAS-SAS", "yes"),
                               ("KXNFLGAME-26OCT25WASDAL-WAS", "yes")])
    assert reg.estimate(s, {}, NOW).insufficient


def test_joint_rejects_no_sides():
    assert nba_same_game_joint([{"kind": "winner", "team": "SAS", "side": "no", "p": .3, "lo": .2,
                                 "hi": .4, "thr": 0}]) is None


def test_espn_and_odds_api_parsers():
    espn = {"events": [{"competitions": [{"date": "2026-10-23T23:00Z", "competitors": [
        {"homeAway": "home", "team": {"abbreviation": "BOS"}},
        {"homeAway": "away", "team": {"abbreviation": "NY"}}],
        "odds": [{"provider": {"name": "DraftKings"},
                  "moneyline": {"home": {"close": {"odds": "-130"}}, "away": {"close": {"odds": "+110"}}},
                  "pointSpread": {"home": {"close": {"line": "-2.5", "odds": "-110"}},
                                  "away": {"close": {"line": "+2.5", "odds": "-110"}}},
                  "total": {"over": {"close": {"line": "o223.5", "odds": "-110"}},
                            "under": {"close": {"line": "u223.5", "odds": "-110"}}}}]}]}]}
    g = parse_espn_scoreboard(espn, NOW)[0]
    b = g.books[0]
    assert (g.home, g.away, b.ml_home, b.spread_home, b.total) == ("BOS", "NYK", -130, -2.5, 223.5)
    oa = [{"home_team": "Boston Celtics", "away_team": "New York Knicks", "commence_time": "2026-10-23T23:00:00Z",
           "bookmakers": [{"key": "pinnacle", "title": "Pinnacle", "last_update": "2026-10-21T21:00:00Z",
                           "markets": [{"key": "h2h", "outcomes": [{"name": "Boston Celtics", "price": -128},
                                                                   {"name": "New York Knicks", "price": 116}]},
                                       {"key": "totals", "outcomes": [{"name": "Over", "price": -108, "point": 223},
                                                                      {"name": "Under", "price": -112, "point": 223}]}]}]}]
    g = parse_odds_api(oa)[0]
    assert g.books[0].book == "Pinnacle" and g.books[0].ml_away == 116 and g.books[0].total == 223
