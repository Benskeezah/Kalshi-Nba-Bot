"""Offline player-props demo (no network needed).

    python examples/props_demo.py     # prints reports, writes props_report.html

  props_2026-10-21_dalhou.json  REAL Kalshi prop ladders for DAL @ HOU, Oct 21 2026
  evidence_props.json           REAL prices found in betting articles that day:
                                FanDuel VanVleet u5.5 assists +118; VanVleet 5+ assists -174;
                                DraftKings game lines
  props_parlays_mock.json       MOCK parlays (invented prices) built from the real props
Players with no line in the evidence file come back INSUFFICIENT DATA here. Live, with
ODDS_API_KEY set, the tool pulls every book's prop lines and ESPN game logs itself.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from kalshi_edge.estimators.registry import EstimatorRegistry
from kalshi_edge.external.nba_stats import StatsDB
from kalshi_edge.highprob import high_prob_picks, text_picks
import make_mock_stats
from kalshi_edge.external.evidence_file import EvidenceFile
from kalshi_edge.ingestion.kalshi import FixtureKalshiClient
from kalshi_edge.pipeline import scan
from kalshi_edge.report import html_report, text_report, text_table

F = os.path.join(os.path.dirname(__file__), "..", "fixtures")
NOW = datetime(2026, 10, 21, 22, 15, tzinfo=timezone.utc)

client = FixtureKalshiClient([os.path.join(F, "props_2026-10-21_dalhou.json"),
                              os.path.join(F, "props_parlays_mock.json")])
evidence = EvidenceFile(os.path.join(F, "evidence_props.json"))
# MOCK stats database (invented box scores) so the matchup/minutes/pace model has
# something to work with offline. Live, use: python -m kalshi_edge build-stats
mock_db = os.environ.get("MOCK_STATS_DB", "mock_stats.sqlite")
if os.path.exists(mock_db):
    os.remove(mock_db)
make_mock_stats.build(mock_db)
registry = EstimatorRegistry(use_live_sources=False, evidence=evidence, stats_db=StatsDB(mock_db), stats_mock=True)
results = scan(client.markets(), registry, evidence, store=None, contracts=10, now=NOW)
picks = high_prob_picks(results, 0.70, allow_low=True)

print(text_table(results), "\n")
print(text_picks(picks, 0.70), "\n")
for a in results:
    print(text_report(a), "\n" + "=" * 78 + "\n")
out = os.environ.get("DEMO_HTML", "props_report.html")
with open(out, "w") as f:
    f.write(html_report(results, "NBA Props Scan", picks=picks, min_prob=0.70, note=(
        "DEMO. Kalshi prop prices are real (Mavericks at Rockets, Oct 21, 2026, about 2 hours before tip), and so "
        "are the two VanVleet sportsbook prices. The player and team STATS are MOCK (invented) because this build "
        "environment can't reach ESPN, so the projections and picks below are NOT real advice. They show what "
        "the tool produces once you run 'build-stats' on your own machine. Low-confidence picks are shown "
        "here only to demonstrate the table.")))
print("wrote", out)
