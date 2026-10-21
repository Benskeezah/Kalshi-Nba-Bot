"""Offline NBA demo (no network needed).

    python examples/nba_demo.py      # prints reports, writes nba_report.html

  nba_2026-10-23.json   REAL Kalshi prices for Oct 23 games (winner, margin, total), captured Oct 21
  evidence_nba.json     REAL DraftKings lines for NYK@BOS and HOU@SA from ESPN, read Oct 21
  nba_parlays_mock.json MOCK parlay markets (invented prices) built from the real legs above
LAC@LAL has a real Kalshi market but no line was available -> INSUFFICIENT DATA.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_edge.estimators.registry import EstimatorRegistry
from kalshi_edge.external.evidence_file import EvidenceFile
from kalshi_edge.ingestion.kalshi import FixtureKalshiClient
from kalshi_edge.pipeline import scan
from kalshi_edge.report import html_report, text_report, text_table

F = os.path.join(os.path.dirname(__file__), "..", "fixtures")
NOW = datetime(2026, 10, 21, 21, 45, tzinfo=timezone.utc)

client = FixtureKalshiClient([os.path.join(F, "nba_2026-10-23.json"), os.path.join(F, "nba_parlays_mock.json")])
evidence = EvidenceFile(os.path.join(F, "evidence_nba.json"))
registry = EstimatorRegistry(use_live_sources=False, evidence=evidence)
results = scan(client.markets(), registry, evidence, store=None, contracts=10, now=NOW)

print(text_table(results), "\n")
for a in results:
    print(text_report(a), "\n" + "=" * 78 + "\n")
out = os.environ.get("DEMO_HTML", "nba_report.html")
with open(out, "w") as f:
    f.write(html_report(results, "NBA Edge Scan", note=(
        "Kalshi prices are real (captured Oct 21, 2026 for games on Oct 23). The only sportsbook evidence is "
        "DraftKings via ESPN, also real. Markets whose title starts with MOCK are invented parlays used to test "
        "leg handling; their prices are not real.")))
print("wrote", out)
