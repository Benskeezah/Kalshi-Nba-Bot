import math
from datetime import datetime, timezone

import pytest

from kalshi_edge.estimators.nba_props import (fair_over_probs, nb_sf, p_over_line, parse_prop_ticker,
                                              prop_same_game_joint, r_from_log, solve_mu)
from kalshi_edge.estimators.registry import EstimatorRegistry
from kalshi_edge.external.evidence_file import EvidenceFile
from kalshi_edge.external.nba_props import (PropLine, PropProvider, norm_name, parse_espn_gamelog,
                                            parse_odds_api_props)
from kalshi_edge.ingestion.kalshi import normalize_market

NOW = datetime(2026, 10, 21, 22, 15, tzinfo=timezone.utc)
EV = {"nba_props": [{"player": "Fred VanVleet", "stat": "assists", "line": 5.5, "over": -130, "under": 110,
                     "book": "FanDuel", "as_of": "2026-10-21T21:00:00Z"}],
      "nba_lines": [{"home": "HOU", "away": "DAL", "tip": "2026-10-22T00:30:00Z", "book": "DraftKings",
                     "as_of": "2026-10-21T21:00:00Z", "ml_home": -375, "ml_away": 295}]}


def spec(ticker, title="", legs=()):
    m = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": title, "strike_type": "greater",
         "mve_selected_legs": [{"market_ticker": t, "side": "yes"} for t in legs]}
    return normalize_market(m, "t", NOW).contract_spec()


def test_nb_matches_poisson_for_large_r():
    mu = 6.0
    pois = 1 - sum(math.exp(-mu) * mu ** i / math.factorial(i) for i in range(6))
    assert nb_sf(6, mu, 1e6) == pytest.approx(pois, abs=1e-4)
    assert nb_sf(0, mu, 10) == 1.0


def test_solve_mu_inverts_line():
    mu = solve_mu(24.5, 0.55, 14)
    assert p_over_line(24.5, mu, 14) == pytest.approx(0.55, abs=1e-4)
    # integer line: pushes excluded
    mu2 = solve_mu(25, 0.5, 14)
    assert p_over_line(25, mu2, 14) == pytest.approx(0.5, abs=1e-4)


def test_one_sided_prices_give_a_range():
    qs = fair_over_probs(PropLine("x", "assists", 5.5, None, 118, "FD", None))
    assert len(qs) == 2 and qs[0] < qs[1] and 0.55 < qs[0] < 0.58


def test_prop_ticker_and_names():
    t = parse_prop_ticker("KXNBAPTS-26OCT21DALHOU-HOUKDURANT7-25")
    assert (t["stat"], t["team"], t["name_code"], t["jersey"], t["k"]) == ("points", "HOU", "KDURANT", "7", 25)
    assert parse_prop_ticker("KXNBAPTS-26OCT20OKCSAS-OKCSGILGEOUSALEXANDER2-40")["name_code"] == "SGILGEOUSALEXANDER"
    assert norm_name("Jabari Smith Jr.") == norm_name("jabari smith")
    pp = PropProvider(False, [{"player": "Jabari Smith Jr.", "stat": "points", "line": 14.5, "book": "x"}])
    assert pp.player_for_code("JSMITH", "HOU") == "Jabari Smith Jr."


def test_ladder_is_monotonic_and_matches_line():
    reg = EstimatorRegistry(False, EvidenceFile(data=EV))
    ps = [reg.estimate(spec(f"KXNBAAST-26OCT21DALHOU-HOUFVANVLEET5-{k}", f"Fred VanVleet: {k}+ assists"), {}, NOW)
          for k in (2, 4, 6, 8, 10)]
    vals = [e.p_yes for e in ps]
    assert vals == sorted(vals, reverse=True)
    from kalshi_edge.estimators.nba import devig
    assert ps[2].p_yes == pytest.approx(0.5 * (devig(-130, 110) + devig(-130, 110, "power")), abs=0.01)


def test_props_insufficient_without_evidence_and_after_tip():
    reg = EstimatorRegistry(False, EvidenceFile(data=EV))
    assert reg.estimate(spec("KXNBAPTS-26OCT21DALHOU-HOUKDURANT7-25", "Kevin Durant: 25+ points"), {}, NOW).insufficient
    late = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)
    assert reg.estimate(spec("KXNBAAST-26OCT21DALHOU-HOUFVANVLEET5-6", "Fred VanVleet: 6+ assists"), {}, late).insufficient


def test_prop_parlays():
    reg = EstimatorRegistry(False, EvidenceFile(data=EV))
    a4, a6 = "KXNBAAST-26OCT21DALHOU-HOUFVANVLEET5-4", "KXNBAAST-26OCT21DALHOU-HOUFVANVLEET5-6"
    p6 = reg.estimate(spec(a6, "Fred VanVleet: 6+ assists"), {}, NOW).p_yes
    par = reg.estimate(spec("KXMVEX-P1", legs=[a4, a6]), {}, NOW)
    assert par.p_yes == pytest.approx(p6, abs=1e-6)                     # ladder collapses
    mixed = reg.estimate(spec("KXMVEX-P2", legs=[a6, "KXNBAGAME-26OCT21DALHOU-HOU"]), {}, NOW)
    assert mixed.confidence == "LOW" and any("correlated" in o for o in mixed.opposing)


def test_joint_two_players_is_inexact():
    c = lambda pl, thr, p: {"kind": "prop", "player": pl, "stat": "points", "side": "yes", "thr": thr,
                            "p": p, "lo": p - .05, "hi": p + .05}
    p, *_, exact = prop_same_game_joint([c("A", 20, .6), c("A", 25, .4), c("B", 15, .5)])
    assert p == pytest.approx(0.2) and exact is False


def test_r_from_log():
    assert r_from_log([5.0] * 20, 30) == 200.0            # no over-dispersion
    assert r_from_log([1, 2, 3], 30) == 30                # too few games -> default
    vals = [10, 30] * 10                                  # mean 20, var 100 -> r = 400/80
    assert r_from_log(vals, 14) == pytest.approx(5.0)


def test_parsers():
    ev = {"bookmakers": [{"title": "Pinnacle", "last_update": "2026-10-21T21:00:00Z", "markets": [
        {"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Kevin Durant", "price": -115, "point": 26.5},
            {"name": "Under", "description": "Kevin Durant", "price": -105, "point": 26.5}]},
        {"key": "player_points_alternate", "outcomes": [
            {"name": "Over", "description": "Kevin Durant", "price": 160, "point": 30.5}]}]}]}
    ls = parse_odds_api_props(ev)
    assert len(ls) == 2 and ls[0].over == -115 and ls[1].under is None and ls[1].line == 30.5
    gl = {"labels": ["MIN", "FG", "REB", "AST", "PTS"],
          "events": {"1": {"gameDate": "2026-04-10T00:00:00Z"}, "2": {"gameDate": "2026-04-08T00:00:00Z"}},
          "seasonTypes": [{"displayName": "2025-26 Regular Season", "categories": [{"events": [
              {"eventId": "2", "stats": ["34", "8-17", "6", "4", "22"]},
              {"eventId": "1", "stats": ["36", "11-20", "7", "5", "31"]}]}]},
              {"displayName": "2025-26 Postseason", "categories": [{"events": [
                  {"eventId": "9", "stats": ["40", "1-1", "1", "1", "50"]}]}]}]}
    g = parse_espn_gamelog(gl, "Kevin Durant", "points")
    assert g.values == [31.0, 22.0]                       # newest first, playoffs excluded


def test_short_night_lowers_sure_things_most():
    from kalshi_edge.estimators.nba_props import p_at_least
    low_plain, low_mix = nb_sf(2, 4.5, 30), p_at_least(2, 4.5, 30, 0.07)
    high_plain, high_mix = nb_sf(8, 4.5, 30), p_at_least(8, 4.5, 30, 0.07)
    assert low_mix < low_plain and low_plain - low_mix > high_plain - high_mix
    assert p_at_least(2, 4.5, 30, 0.0) == pytest.approx(low_plain)


def test_pattern_tables():
    from kalshi_edge.calibration import pattern_html, pattern_tables
    rows = [{"ticker": "KXNBAAST-26OCT21DALHOU-HOUFVANVLEET5-2", "event_ticker": "KXNBAAST-26OCT21DALHOU",
             "model_prob": 0.95, "outcome": 0, "yes_ask": 0.9, "yes_bid": 0.88, "market_prob": 0.89},
            {"ticker": "KXNBAPTS-26OCT21DALHOU-HOUKDURANT7-35", "event_ticker": "KXNBAPTS-26OCT21DALHOU",
             "model_prob": 0.1, "outcome": 0, "yes_ask": None, "yes_bid": 0.08, "market_prob": 0.1}]
    t = dict(pattern_tables(rows))
    g = {r["group"]: r for r in t["Stat and side"]}
    assert g["Assists YES"]["hit"] == 0 and g["Points NO"]["hit"] == 1
    assert g["Assists YES"]["pnl"] == pytest.approx(-0.9 - 0.07 * 0.9 * 0.1)
    assert "What's hitting" in pattern_html(rows)
