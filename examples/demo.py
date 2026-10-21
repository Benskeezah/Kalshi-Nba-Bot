"""Offline demo over the saved fixtures (no network needed).

    python examples/demo.py            # prints reports, writes demo_report.html

Fixtures:
  kxhighny_2026-10-22.json  REAL Kalshi prices for NYC's Oct 22 high (captured Oct 21)
  evidence_real.json        REAL NWS and yr.no forecasts read the same day
  parlay_2026-10-21.json    REAL parlay market with no evidence -> INSUFFICIENT DATA
  mock_generic.json         MOCK market + mock evidence, to exercise the generic estimator
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
NOW = datetime(2026, 10, 21, 20, 30, tzinfo=timezone.utc)   # when the fixtures were captured

client = FixtureKalshiClient([os.path.join(F, n) for n in
                              ("kxhighny_2026-10-22.json", "parlay_2026-10-21.json", "mock_generic.json")])
real = EvidenceFile(os.path.join(F, "evidence_real.json"))
mock = EvidenceFile(os.path.join(F, "evidence_mock.json"))
evidence = EvidenceFile(data={**real.data, **{k: v for k, v in mock.data.items() if not k.startswith("_")}})
evidence.data["MOCK-RAINOUT-26OCT25"]["_mock"] = True

registry = EstimatorRegistry(use_live_sources=False)
results = scan(client.markets(), registry, evidence, store=None, contracts=10, now=NOW)

print(text_table(results), "\n")
for a in results:
    print(text_report(a), "\n" + "=" * 78 + "\n")
out = os.environ.get("DEMO_HTML", "demo_report.html")
with open(out, "w") as f:
    f.write(html_report(results, "Kalshi Edge Scan", note=(
        "Six NYC high-temperature buckets use real Kalshi prices and real NWS / yr.no forecasts captured "
        "on Oct 21, 2026 (order-book depth was not captured). The parlay is a real market with no evidence "
        "supplied. The rain-out market is MOCK data used only to exercise the generic estimator.")))
print("wrote", out)
